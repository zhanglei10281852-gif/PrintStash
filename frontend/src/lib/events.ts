"use client";

/**
 * The events socket: one shared connection per tab that tells the UI when
 * background work changed, so views refetch instead of polling blind.
 *
 * A notice carries ids and a hint, never data a reader needs authorization for;
 * a listener refetches through the authorized endpoints. Every committed
 * notice also has a durable `seq`: the server keeps a bounded log, so a tab
 * whose socket briefly dropped — a flaky network, an API restart, a worker
 * split over PostgreSQL — sends its last `seq` as `replay_after` and merges
 * the ordered replay page with whatever already arrived live, deduped by
 * `seq`. A cursor past the retention window, ahead of the log, or naming a
 * scope that disappeared answers `resync`, which means "refetch what you show";
 * REST stays the source of truth for cancel, retry and deletion.
 *
 * Channels are implicit for the user's own Jobs (and, for administrators, every
 * Job); a view that shows a Model follows `model:<id>` to hear about its
 * derivatives, and the server acknowledges each channel it accepts with
 * `subscribed`.
 */

import { createEventsTicket } from "@/lib/api/work";
import { getWsUrl } from "@/lib/api/request";
import type { DerivativeState, JobState } from "@/types";

export type ResyncReason = "cursor_expired" | "cursor_ahead" | "scope_revoked";

export type EventNotice =
  | { type: "resync"; reason?: ResyncReason; head?: number }
  | { type: "derivative_policy"; seq?: number }
  | { type: "subscribed"; channel: string }
  | {
      type: "job";
      job_id: string;
      kind: string;
      state: JobState;
      progress: number | null;
      seq?: number;
    }
  | {
      type: "derivative";
      model_id: number;
      file_id: number;
      kind: string;
      state: DerivativeState;
      seq?: number;
    };

type HelloFrame = {
  type: "hello";
  head: number;
  floor: number;
  retention_seconds: number;
};

type ReplayedNotice = Extract<
  EventNotice,
  { type: "job" } | { type: "derivative" } | { type: "derivative_policy" }
>;

/** A row of the durable log always carries the seq it was committed with. */
export type SequencedNotice = ReplayedNotice & { seq: number };

type ReplayFrame = {
  type: "replay";
  events: SequencedNotice[];
  next_cursor: number;
  has_more: boolean;
  head: number;
};

export type ServerFrame = EventNotice | HelloFrame | ReplayFrame;

// Closed sets the server owns; a wire member outside its set invalidates the
// frame instead of widening a closed enum to an arbitrary string.
const RESYNC_REASONS = ["cursor_expired", "cursor_ahead", "scope_revoked"] as const;
const JOB_STATES = [
  "queued",
  "running",
  "interrupted",
  "completed",
  "failed",
  "cancelled",
] as const;
const DERIVATIVE_STATES = ["queued", "running", "ready", "skipped", "failed", "cancelled"] as const;

/**
 * One raw frame's wire shape: the members a frame can carry, in their JSON
 * representation. A parsed object only becomes this at the single boundary
 * cast, and every arm still re-validates presence, closed-set membership and
 * numeric shape before it builds a typed frame.
 */
interface FrameWire {
  type?: string;
  job_id?: string;
  kind?: string;
  state?: string;
  progress?: number | null;
  model_id?: number;
  file_id?: number;
  channel?: string;
  reason?: string;
  head?: number;
  floor?: number;
  retention_seconds?: number;
  next_cursor?: number;
  has_more?: boolean;
  events?: FrameWire[];
  seq?: number;
}

function seqOf(notice: EventNotice): number | undefined {
  if (
    notice.type === "job" ||
    notice.type === "derivative" ||
    notice.type === "derivative_policy"
  ) {
    return notice.seq;
  }
  return undefined;
}

function isSequenced(notice: EventNotice): notice is SequencedNotice {
  if (
    notice.type !== "job" &&
    notice.type !== "derivative" &&
    notice.type !== "derivative_policy"
  ) {
    return false;
  }
  return Number.isInteger(notice.seq);
}

export type EventListener = (notice: EventNotice) => void;

/** The subset of `WebSocket` the client uses, so tests can drive a fake. */
export interface EventSocket {
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  send(data: string): void;
  close(): void;
}

export type EventSocketFactory = () => Promise<EventSocket>;

const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const CURSOR_STORAGE = "printstash.events.cursor";

async function openEventSocket(): Promise<EventSocket> {
  const { ticket } = await createEventsTicket();
  const ws = new WebSocket(getWsUrl(`/api/v1/events/ws?ticket=${encodeURIComponent(ticket)}`));
  const adapter: EventSocket = {
    onopen: null,
    onclose: null,
    onmessage: null,
    send: (data) => ws.send(data),
    close: () => ws.close(),
  };
  ws.onopen = () => adapter.onopen?.();
  ws.onclose = () => adapter.onclose?.();
  // The server only sends text frames.
  ws.onmessage = (event) => adapter.onmessage?.({ data: String(event.data) });
  return adapter;
}

let socketFactory: EventSocketFactory = openEventSocket;
const listeners = new Set<EventListener>();
const followed = new Map<string, number>();
let socket: EventSocket | null = null;
let open = false;
let connecting = false;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let failures = 0;
let head = 0;
let cursor: number | null = null;
let replaying = false;
const buffered: SequencedNotice[] = [];

/** Point the client at a different socket (tests); resets any live connection. */
export function setEventSocketFactory(factory: EventSocketFactory): void {
  disconnect();
  socketFactory = factory;
  failures = 0;
}

function isBrowser(): boolean {
  return "window" in globalThis;
}

function loadCursor(): number | null {
  if (!isBrowser()) return null;
  try {
    const stored = window.localStorage.getItem(CURSOR_STORAGE);
    if (stored === null) return null;
    const value = Number(stored);
    return Number.isInteger(value) && value >= 0 ? value : null;
  } catch {
    return null;
  }
}

function saveCursor(value: number): void {
  cursor = value;
  if (!isBrowser()) return;
  try {
    window.localStorage.setItem(CURSOR_STORAGE, String(value));
  } catch {
    // Storage disabled: this tab still merges by memory, reconnect only loses
    // the cursor and refetches.
  }
}

function deliver(notice: EventNotice): void {
  for (const listener of listeners) listener(notice);
}

/** Identity match for the wire's string members without a runtime typeof. */
function asText(member: string | undefined): string | null {
  if (member === undefined) return null;
  return Object.is(String(member), member) ? member : null;
}

/** Match a string member against the closed set it must belong to. */
function asMember<T extends string>(member: string | undefined, allowed: readonly T[]): T | null {
  const decoded = asText(member);
  if (decoded === null) return null;
  const match = allowed.find((candidate) => candidate === decoded);
  return match === undefined ? null : match;
}

/** An integer member; absent, fractional or coerced values are rejected. */
function asInteger(member: number | undefined): number | null {
  if (member === undefined || !Object.is(Number(member), member)) return null;
  return Number.isInteger(member) ? member : null;
}

function decodeJob(wire: FrameWire): EventNotice | null {
  const job_id = asText(wire.job_id);
  const kind = asText(wire.kind);
  const state = asMember(wire.state, JOB_STATES);
  if (job_id === null || kind === null || state === null) return null;
  let progress: number | null = null;
  if (wire.progress !== null && wire.progress !== undefined) {
    const decoded = Number(wire.progress);
    if (!Object.is(decoded, wire.progress)) return null;
    progress = decoded;
  }
  const notice: Extract<EventNotice, { type: "job" }> = {
    type: "job",
    job_id,
    kind,
    state,
    progress,
  };
  if (wire.seq !== undefined) {
    const seq = asInteger(wire.seq);
    if (seq === null) return null;
    notice.seq = seq;
  }
  return notice;
}

function decodeDerivative(wire: FrameWire): EventNotice | null {
  const model_id = asInteger(wire.model_id);
  const file_id = asInteger(wire.file_id);
  const kind = asText(wire.kind);
  const state = asMember(wire.state, DERIVATIVE_STATES);
  if (model_id === null || file_id === null || kind === null || state === null) {
    return null;
  }
  const notice: Extract<EventNotice, { type: "derivative" }> = {
    type: "derivative",
    model_id,
    file_id,
    kind,
    state,
  };
  if (wire.seq !== undefined) {
    const seq = asInteger(wire.seq);
    if (seq === null) return null;
    notice.seq = seq;
  }
  return notice;
}

function decodeReplay(wire: FrameWire): ReplayFrame | null {
  const next_cursor = asInteger(wire.next_cursor);
  const head = asInteger(wire.head);
  if (next_cursor === null || head === null) return null;
  if (wire.has_more !== true && wire.has_more !== false) return null;
  if (!Array.isArray(wire.events)) return null;
  const events: SequencedNotice[] = [];
  for (const record of wire.events) {
    // Scalars cannot be frames; an array-shaped entry decodes to nothing.
    if (!(record instanceof Object)) continue;
    const event = decodeFrame(record);
    if (event === null) continue;
    if (event.type !== "hello" && event.type !== "replay" && isSequenced(event)) {
      events.push(event);
    }
  }
  return { type: "replay", events, next_cursor, has_more: wire.has_more, head };
}

function decodeFrame(wire: FrameWire): ServerFrame | null {
  // The server writes every frame from the events protocol; a frame with an
  // unknown type or member (a newer server) is ignored rather than guessed at.
  switch (asText(wire.type)) {
    case "hello": {
      const head = asInteger(wire.head);
      const floor = asInteger(wire.floor);
      const retention_seconds = asInteger(wire.retention_seconds);
      if (head === null || floor === null || retention_seconds === null) return null;
      return { type: "hello", head, floor, retention_seconds };
    }
    case "replay":
      return decodeReplay(wire);
    case "resync": {
      const notice: Extract<EventNotice, { type: "resync" }> = { type: "resync" };
      const reason = asMember(wire.reason, RESYNC_REASONS);
      const head = asInteger(wire.head);
      if (reason !== null) notice.reason = reason;
      if (head !== null) notice.head = head;
      return notice;
    }
    case "subscribed": {
      const channel = asText(wire.channel);
      return channel === null ? null : { type: "subscribed", channel };
    }
    case "job":
      return decodeJob(wire);
    case "derivative":
      return decodeDerivative(wire);
    case "derivative_policy": {
      const notice: Extract<EventNotice, { type: "derivative_policy" }> = {
        type: "derivative_policy",
      };
      const seq = asInteger(wire.seq);
      if (seq !== null) notice.seq = seq;
      return notice;
    }
    default:
      return null;
  }
}

function parse(data: string): ServerFrame | null {
  try {
    const parsed: unknown = JSON.parse(data);
    if (!(parsed instanceof Object) || Array.isArray(parsed)) return null;
    // SAFETY: proven a non-array JSON object; the interface's members are all
    // optional, and each decoder validates the ones belonging to its frame.
    return decodeFrame(parsed as FrameWire);
  } catch {
    return null;
  }
}

function send(
  message: { subscribe: string } | { unsubscribe: string } | { replay_after: number },
): void {
  if (socket && open) socket.send(JSON.stringify(message));
}

function handleLive(notice: EventNotice): void {
  if (replaying) {
    // Hold live frames until the replay page lands; merge dedupes the overlap.
    if (isSequenced(notice)) buffered.push(notice);
    else deliver(notice);
    return;
  }
  const seq = seqOf(notice);
  if (seq !== undefined && cursor !== null && seq <= cursor) {
    // Already recovered through an earlier replay or refetch.
    return;
  }
  deliver(notice);
  if (seq !== undefined && (cursor === null || seq > cursor)) {
    saveCursor(seq);
  }
}

function resync(reason: ResyncReason | undefined, at: number | undefined): void {
  // The durable window cannot answer: refetch current state and restart the
  // cursor at the server's head, so later live frames are not dropped as
  // duplicates of what the refetch already showed.
  replaying = false;
  buffered.length = 0;
  saveCursor(at ?? head);
  deliver({ type: "resync" });
}

function finishReplay(frame: ReplayFrame): void {
  const merged: SequencedNotice[] = [...buffered, ...frame.events];
  buffered.length = 0;
  const bySeq = new Map<number, SequencedNotice>();
  for (const notice of merged) {
    if (cursor !== null && notice.seq <= cursor) continue;
    // First copy wins: a live frame and its replay are the same event.
    if (!bySeq.has(notice.seq)) bySeq.set(notice.seq, notice);
  }
  const ordered = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  for (const notice of ordered) {
    deliver(notice);
    saveCursor(notice.seq);
  }
  if (frame.next_cursor > (cursor ?? 0)) saveCursor(frame.next_cursor);
  if (frame.has_more) {
    send({ replay_after: frame.next_cursor });
    return;
  }
  replaying = false;
}

function handleFrame(frame: ServerFrame): void {
  if (frame.type === "hello") {
    head = frame.head;
    return;
  }
  if (frame.type === "replay") {
    finishReplay(frame);
    return;
  }
  if (frame.type === "resync") {
    const { reason, head: at } = frame;
    if (reason !== undefined && replaying) {
      // Cursor expired, cursor ahead, or scope revoked: the page is refused.
      resync(reason, at);
    } else if (reason === undefined && replaying) {
      // The first-connect resync, sent while our replay request is in flight.
      return;
    } else if (reason === undefined && cursor !== null) {
      // A best-effort transport reconnect (e.g. NOTIFY re-listen): fill the
      // gap from the log instead of asking every view to refetch blind.
      replaying = true;
      send({ replay_after: cursor });
    } else {
      // First connect, or already past the window: refetch is the recovery.
      resync(reason, at);
    }
    return;
  }
  if (frame.type === "subscribed") {
    deliver(frame);
    return;
  }
  handleLive(frame);
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null || listeners.size === 0) return;
  const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** failures);
  failures += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect();
  }, delay);
}

async function connect(): Promise<void> {
  if (socket || connecting || listeners.size === 0 || !isBrowser()) return;
  connecting = true;
  let next: EventSocket;
  try {
    next = await socketFactory();
  } catch {
    connecting = false;
    scheduleReconnect();
    return;
  }
  connecting = false;
  if (listeners.size === 0) {
    next.close();
    return;
  }
  socket = next;
  next.onopen = () => {
    open = true;
    failures = 0;
    for (const channel of followed.keys()) send({ subscribe: channel });
    // Read at open, not at module load: another tab or an older build's
    // persisted cursor counts even when this module initialized beforehand.
    const stored = loadCursor();
    if (stored !== null) {
      cursor = stored;
      replaying = true;
      send({ replay_after: cursor });
    }
  };
  next.onmessage = (event) => {
    const frame = parse(event.data);
    if (frame) handleFrame(frame);
  };
  next.onclose = () => {
    if (socket !== next) return;
    socket = null;
    open = false;
    replaying = false;
    buffered.length = 0;
    scheduleReconnect();
  };
}

function disconnect(): void {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const current = socket;
  socket = null;
  open = false;
  replaying = false;
  buffered.length = 0;
  current?.close();
}

/** Hear every notice this tab receives; the socket lives while anyone listens. */
export function subscribeEvents(listener: EventListener): () => void {
  if (!isBrowser()) return () => {};
  listeners.add(listener);
  void connect();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) disconnect();
  };
}

/**
 * Hear about one Model's derivatives (a placeholder becoming a thumbnail),
 * every `resync`, and the server's `subscribed` acknowledgement of this
 * Model's channel: a derivative that settled before the subscription took
 * effect was announced to nobody, so a follower refetches on that
 * acknowledgement. The server checks the viewer may see the Model.
 */
export function followModel(modelId: number, listener: EventListener): () => void {
  const channel = `model:${modelId}`;
  const filtered: EventListener = (notice) => {
    if (
      notice.type === "resync" ||
      notice.type === "derivative_policy" ||
      (notice.type === "subscribed" && notice.channel === channel) ||
      (notice.type === "derivative" && notice.model_id === modelId)
    )
      listener(notice);
  };
  const unsubscribe = subscribeEvents(filtered);
  const count = followed.get(channel) ?? 0;
  followed.set(channel, count + 1);
  if (count === 0) send({ subscribe: channel });
  return () => {
    unsubscribe();
    const remaining = (followed.get(channel) ?? 1) - 1;
    if (remaining > 0) {
      followed.set(channel, remaining);
      return;
    }
    followed.delete(channel);
    send({ unsubscribe: channel });
  };
}
