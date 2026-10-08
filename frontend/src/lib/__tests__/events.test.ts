/*
 * The events socket every view shares to hear that background work changed.
 *
 * One connection per tab, opened while anything listens and closed when
 * nothing does: a socket per component would multiply server load by the
 * number of mounted views. It reconnects with a bounded backoff and replays
 * its durable cursor — merging the page with frames that arrived live,
 * deduped by seq — instead of waiting for polling. A frame it does not
 * understand is ignored rather than guessed at; a newer server may send
 * kinds this build predates.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EventNotice, EventSocket, SequencedNotice, ServerFrame } from "@/lib/events";

type Events = typeof import("@/lib/events");

const CURSOR_STORAGE = "printstash.events.cursor";

class FakeSocket implements EventSocket {
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  send = vi.fn<(data: string) => void>();
  close = vi.fn<() => void>();

  open(): void {
    this.onopen?.();
  }

  frame(data: string | ServerFrame): void {
    this.onmessage?.({ data: data instanceof Object ? JSON.stringify(data) : data });
  }

  drop(): void {
    this.onclose?.();
  }

  sent(): string[] {
    return this.send.mock.calls.map(([data]) => data);
  }
}

let events: Events;
let sockets: FakeSocket[];
const opened = vi.fn<() => Promise<EventSocket>>();

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  window.localStorage.clear();
  sockets = [];
  opened.mockReset();
  opened.mockImplementation(async () => {
    const socket = new FakeSocket();
    sockets.push(socket);
    return socket;
  });
  events = await import("@/lib/events");
  events.setEventSocketFactory(opened);
});

afterEach(() => {
  vi.useRealTimers();
  window.localStorage.clear();
});

/** Let the factory's promise resolve and the socket open. */
async function connected(): Promise<FakeSocket> {
  await vi.advanceTimersByTimeAsync(0);
  const socket = sockets.at(-1)!;
  socket.open();
  return socket;
}

/** Hello plus the first-connect resync the server always sends. */
function greet(socket: FakeSocket, head = 0): void {
  socket.frame({ type: "hello", head, floor: 0, retention_seconds: 86400 });
  socket.frame({ type: "resync" });
}

const job = (seq: number, state: "running" | "completed" = "running"): SequencedNotice => ({
  type: "job",
  job_id: `j${seq}`,
  kind: "ingestion.upload",
  state,
  progress: 40,
  seq,
});

describe("subscribeEvents", () => {
  it("delivers each live notice the server sends", async () => {
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();

    socket.frame({
      type: "job",
      job_id: "j1",
      kind: "ingestion.upload",
      state: "running",
      progress: 40,
    });

    expect(heard).toHaveBeenCalledWith({
      type: "job",
      job_id: "j1",
      kind: "ingestion.upload",
      state: "running",
      progress: 40,
    });
  });

  it("advances the cursor from a live notice", async () => {
    events.subscribeEvents(vi.fn());
    const socket = await connected();

    socket.frame(job(4));

    expect(window.localStorage.getItem(CURSOR_STORAGE)).toBe("4");
  });

  it("shares one connection between listeners", async () => {
    events.subscribeEvents(vi.fn());
    events.subscribeEvents(vi.fn());
    await connected();

    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("closes the connection when the last listener leaves", async () => {
    const first = events.subscribeEvents(vi.fn());
    const second = events.subscribeEvents(vi.fn());
    const socket = await connected();

    first();
    expect(socket.close).not.toHaveBeenCalled();
    second();

    expect(socket.close).toHaveBeenCalledTimes(1);
  });

  it.each([
    { label: "an unknown kind", frame: JSON.stringify({ type: "hologram" }) },
    { label: "a frame that is not JSON", frame: "not json" },
  ])("ignores $label", async ({ frame }) => {
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();

    socket.frame(frame);

    expect(heard).not.toHaveBeenCalled();
  });

  it("reconnects after the connection drops", async () => {
    events.subscribeEvents(vi.fn());
    const first = await connected();

    first.drop();
    await vi.advanceTimersByTimeAsync(1_000);

    expect(opened).toHaveBeenCalledTimes(2);
  });

  it("backs off while the server stays unreachable", async () => {
    opened.mockRejectedValue(new Error("offline"));
    events.subscribeEvents(vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(opened).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1_000);
    expect(opened).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(opened).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);

    expect(opened).toHaveBeenCalledTimes(3);
  });

  it("closes a connection that opens after the last listener left", async () => {
    const stop = events.subscribeEvents(vi.fn());
    stop();

    await vi.advanceTimersByTimeAsync(0);

    expect(sockets).toHaveLength(1);
    expect(sockets[0].close).toHaveBeenCalledTimes(1);
  });

  it("stops reconnecting once nobody listens", async () => {
    const stop = events.subscribeEvents(vi.fn());
    const socket = await connected();
    socket.drop();

    stop();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("still refetches on the first resync without a cursor", async () => {
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();

    greet(socket);

    expect(heard).toHaveBeenCalledWith({ type: "resync" });
  });
});

describe("cursor replay", () => {
  it("asks for replay from the stored cursor on a new connection", async () => {
    window.localStorage.setItem(CURSOR_STORAGE, "10");
    events.subscribeEvents(vi.fn());
    const socket = await connected();

    expect(socket.sent()).toContain(JSON.stringify({ replay_after: 10 }));
  });

  it("merges a replay page with the live overlap in order, once per seq", async () => {
    window.localStorage.setItem(CURSOR_STORAGE, "10");
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();

    socket.frame(job(12, "completed")); // the same event arriving live early
    socket.frame({ type: "hello", head: 12, floor: 1, retention_seconds: 86400 });
    socket.frame({
      type: "replay",
      events: [job(11, "running"), job(12, "completed")],
      next_cursor: 12,
      has_more: false,
      head: 12,
    });

    const notices = heard.mock.calls
      .map(([notice]) => notice)
      .filter(
        (notice): notice is Extract<SequencedNotice, { type: "job" }> => notice.type === "job",
      );
    expect(notices.map((notice) => notice.seq)).toEqual([11, 12]);
    expect(notices.at(-1)?.state).toBe("completed");
    expect(window.localStorage.getItem(CURSOR_STORAGE)).toBe("12");

    socket.frame(job(13, "completed"));
    expect(
      heard.mock.calls.map(([notice]) => notice).filter((notice) => notice.type === "job"),
    ).toHaveLength(3);
    expect(window.localStorage.getItem(CURSOR_STORAGE)).toBe("13");
  });

  it("restarts the cursor at the server head when it expired", async () => {
    window.localStorage.setItem(CURSOR_STORAGE, "5");
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();
    socket.frame({ type: "hello", head: 40, floor: 30, retention_seconds: 86400 });
    socket.frame({ type: "resync", reason: "cursor_expired", head: 40 });

    expect(heard).toHaveBeenCalledWith({ type: "resync" });
    expect(window.localStorage.getItem(CURSOR_STORAGE)).toBe("40");

    socket.frame(job(41, "completed"));
    expect(heard).toHaveBeenCalledWith(expect.objectContaining({ seq: 41 }));
  });

  it("refetches on a cursor ahead of the log", async () => {
    window.localStorage.setItem(CURSOR_STORAGE, "50");
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();
    socket.frame({ type: "hello", head: 9, floor: 1, retention_seconds: 86400 });
    socket.frame({ type: "resync", reason: "cursor_ahead", head: 9 });

    expect(heard).toHaveBeenCalledWith({ type: "resync" });
    expect(window.localStorage.getItem(CURSOR_STORAGE)).toBe("9");
  });

  it("pages while the server reports more events", async () => {
    window.localStorage.setItem(CURSOR_STORAGE, "0");
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();

    socket.frame({
      type: "replay",
      events: [job(1)],
      next_cursor: 1,
      has_more: true,
      head: 2,
    });
    expect(socket.sent()).toContain(JSON.stringify({ replay_after: 1 }));

    socket.frame({
      type: "replay",
      events: [job(2)],
      next_cursor: 2,
      has_more: false,
      head: 2,
    });

    expect(
      heard.mock.calls
        .map(([notice]) => notice)
        .filter(
          (notice): notice is Extract<SequencedNotice, { type: "job" }> => notice.type === "job",
        )
        .map((notice) => notice.seq),
    ).toEqual([1, 2]);
    expect(window.localStorage.getItem(CURSOR_STORAGE)).toBe("2");
  });

  it("swallows the initial resync while replay is answering", async () => {
    window.localStorage.setItem(CURSOR_STORAGE, "3");
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    const socket = await connected();

    socket.frame({ type: "hello", head: 4, floor: 1, retention_seconds: 86400 });
    socket.frame({ type: "resync" });
    expect(heard).not.toHaveBeenCalled();

    socket.frame({
      type: "replay",
      events: [job(4, "completed")],
      next_cursor: 4,
      has_more: false,
      head: 4,
    });
    expect(
      heard.mock.calls.map(([notice]) => notice).filter((notice) => notice.type === "resync"),
    ).toHaveLength(0);
  });

  it("replays again after a transport reconnect", async () => {
    events.subscribeEvents(vi.fn());
    const first = await connected();
    greet(first);
    first.frame(job(9, "completed"));

    first.drop();
    await vi.advanceTimersByTimeAsync(1_000);
    const second = sockets.at(-1)!;
    second.open();

    expect(second.sent()).toContain(JSON.stringify({ replay_after: 9 }));
    second.frame({
      type: "replay",
      events: [job(9, "completed")],
      next_cursor: 9,
      has_more: false,
      head: 9,
    });
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    expect(
      heard.mock.calls.map(([notice]) => notice).filter((notice) => notice.type === "job"),
    ).toHaveLength(0);
  });
});

describe("followModel", () => {
  it("subscribes to the Model's channel once the socket is open", async () => {
    events.followModel(3, vi.fn());
    const socket = await connected();

    expect(socket.sent()).toEqual([JSON.stringify({ subscribe: "model:3" })]);
  });

  it("hears only its own Model's derivatives", async () => {
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.followModel(3, heard);
    const socket = await connected();

    socket.frame({
      type: "derivative",
      model_id: 4,
      file_id: 1,
      kind: "thumbnail",
      state: "ready",
    });
    socket.frame({
      type: "derivative",
      model_id: 3,
      file_id: 2,
      kind: "thumbnail",
      state: "ready",
      seq: 5,
    });

    expect(heard).toHaveBeenCalledTimes(1);
    expect(heard).toHaveBeenCalledWith(
      expect.objectContaining({ model_id: 3, file_id: 2, seq: 5 }),
    );
  });

  it("hears a resync", async () => {
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.followModel(3, heard);
    const socket = await connected();

    socket.frame({ type: "resync" });

    expect(heard).toHaveBeenCalledWith({ type: "resync" });
  });

  it("follows the Model again on a new connection", async () => {
    events.followModel(3, vi.fn());
    const first = await connected();
    first.drop();
    await vi.advanceTimersByTimeAsync(1_000);

    const second = sockets.at(-1)!;
    second.open();

    expect(second.sent()).toEqual([JSON.stringify({ subscribe: "model:3" })]);
  });

  it("unsubscribes when the last view of the Model leaves", async () => {
    const first = events.followModel(3, vi.fn());
    const second = events.followModel(3, vi.fn());
    events.subscribeEvents(vi.fn());
    const socket = await connected();

    first();
    expect(socket.sent()).not.toContain(JSON.stringify({ unsubscribe: "model:3" }));
    second();

    expect(socket.sent()).toContain(JSON.stringify({ unsubscribe: "model:3" }));
  });
});

describe("the default connection", () => {
  class FakeWebSocket {
    static last: FakeWebSocket | null = null;
    onopen: (() => void) | null = null;
    onclose: (() => void) | null = null;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    send = vi.fn<(data: string) => void>();
    close = vi.fn<() => void>();

    constructor(readonly url: string) {
      FakeWebSocket.last = this;
    }
  }

  beforeEach(async () => {
    vi.resetModules();
    window.localStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ ticket: "t/1", expires_in: 30 }), {
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);
    events = await import("@/lib/events");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("relays frames from a ticketed events socket", async () => {
    const heard = vi.fn<(notice: EventNotice) => void>();
    events.subscribeEvents(heard);
    events.followModel(3, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    const ws = FakeWebSocket.last!;

    ws.onopen?.();
    ws.onmessage?.({
      data: JSON.stringify({ type: "hello", head: 0, floor: 0, retention_seconds: 86400 }),
    });
    ws.onmessage?.({ data: JSON.stringify({ type: "resync" }) });

    expect(ws.url).toMatch(/\/api\/v1\/events\/ws\?ticket=t%2F1$/);
    expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ subscribe: "model:3" }));
    expect(heard).toHaveBeenCalledWith({ type: "resync" });
  });

  it("closes the underlying socket when nobody listens", async () => {
    const stop = events.subscribeEvents(vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    const ws = FakeWebSocket.last!;
    ws.onopen?.();

    stop();

    expect(ws.close).toHaveBeenCalledTimes(1);
  });

  it("reconnects when the underlying socket closes", async () => {
    events.subscribeEvents(vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    const first = FakeWebSocket.last!;
    first.onopen?.();

    first.onclose?.();
    await vi.advanceTimersByTimeAsync(1_000);

    expect(FakeWebSocket.last).not.toBe(first);
  });
});
