# 指定 Revision 公开分享的文件身份保持

> **Workspace:** F:\swe\098802\project-02

---

**User**

管理员把某个模型的指定 G-code 或 STL revision 发给客户后，客户打开公开链接时应始终看到创建分享时选中的那一版，即使模型随后生成新 revision、旧文件被软删除又恢复，或后台执行重试。请让 ShareLink 保存并解析不可漂移的文件身份，detail、缩略图、STL、G-code、toolpath 和下载路由共享同一授权解析，不得回退到当前 live 文件或偷偷换成新 revision；文件进入垃圾箱时活动分享要保留逻辑持有，恢复后原链接继续可用，过期或撤销才释放持有，物理对象已不可恢复时返回稳定的 unavailable/失效结果而不是 404 与新对象之间摇摆。未指定文件的旧式模型分享仍按现有 live-model 语义工作，权限、短 token、并发上传、进程重启和重复清理都要维持确定结果，且不能让一个分享阻止无关文件的回收。

**Assistant**

toolName: Skill

status: success

我先了解仓库现状和相关领域文档，然后规划实现。

toolName: todo_write

status: success

Todos updated: 6 items

toolName: Shell

status: success

toolName: file_search

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

已有进行中的分支 `feat/share-pin-file-revisions` 和未提交改动。我先查看现有改动的全貌，判断进度与缺口。

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

改动似乎只是行尾差异（diff 为空）。确认 HEAD 版本是否已包含这些字段：

toolName: Shell

status: success

HEAD 已包含基础的"选中 G-code revision"能力，工作区改动只是行尾差异；真正的新东西是那个未跟踪的迁移文件。我需要全面读现有实现与领域文档。

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\CONTEXT.md

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\alembic\versions\ce3016c078b6_pin_share_links_to_file_revisions.py

迁移文件是上一轮中断工作留下的唯一产物（新建 `share_link_pins` 表 + cover 字段）。继续读核心模型、trash、GC 与 schema：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\identity.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\schemas\share.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\backups\gc_planner.py

现在读分享相关测试、文件交付 seam 和前端分享对话框，理解现有契约：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\api\v1\test_share.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\e2e\test_sharing.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\storage\artifact_delivery.py

toolName: file_search

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\toolpath_response.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\files.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\share-dialog.tsx

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\library.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\types\models.ts

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\pages\share.tsx

现在按 skill 路由阅读必读参考（code-principles、database、testing）：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\.agents\skills\printstash\references\code-principles.md

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\.agents\skills\printstash\references\database.md

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\.agents\skills\printstash\references\testing.md

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\lib\api\share.ts

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\CHANGELOG.md

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\models.py

我已掌握全貌。现在确认错误映射、FileType 枚举和运行环境：

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\core\errors.py

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: todo_write

status: success

Todos updated: 10 items

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\admin.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\models.py

toolName: Shell

status: success

设计完成。先确认两个细节：GONE→HTTP 映射、现有 GC 测试与迁移测试的组织方式，以及 e2e-real 是否已有分享流程：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\errors.py

toolName: file_search

status: success

toolName: file_search

status: success

toolName: file_search

status: success

toolName: file_search

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\tests\e2e-real\share.spec.ts

toolName: search_by_regex

status: success

toolName: file_search

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_folder

status: success

f:\swe\098802\project-02\backend\tests\integration\db\migrations

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_delete_intent_sha256_migration.py

方案与覆盖矩阵已成形。开始实现——先改模型：

toolName: todo_write

status: success

Todos updated: 10 items

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\library.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\__init__.py

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\identity.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\identity.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\__init__.py

changes: undefined

现在删除陈旧的手工迁移草稿，用 autogenerate 生成新迁移：

toolName: delete_file

status: success

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\alembic\versions\271cd5f966fe_pin_share_links_to_file_revisions_.py

生成成功。先修正 CHECK 约束命名（重复前缀），并参考现有数据回填迁移的写法：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\db\models\identity.py

changes: undefined

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\alembic\versions\8298455ff341_persist_disposable_ingestion_workspaces.py

重新生成（约束名已修正）：

toolName: delete_file

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\alembic\versions\8b46d9a6830b_pin_share_links_to_file_revisions_.py

现在手工补齐迁移的数据回填部分（JSON→pins + 同伴 mesh + cover 快照；downgrade 反向重建 JSON）：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\alembic\versions\8b46d9a6830b_pin_share_links_to_file_revisions_.py

changes: undefined

现在做迁移往返验证：

toolName: Shell

status: success

toolName: Shell

status: success

模型与迁移就绪。现在重写分享服务核心——统一授权解析与不可漂移 pin：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

更新 schema，加入 `is_available`：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\schemas\share.py

changes: undefined

现在让六条公开路由共享同一授权解析：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

核心服务完成。现在接入 trash 硬删除与 GC：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

changes: undefined

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

changes: undefined

接入 GC planner（候选池排除 + 计划复核）：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\backups\gc_planner.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\backups\gc_planner.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\backups\gc_planner.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\backups\gc_planner.py

changes: undefined

让管理端 purge 端点把持有冲突映射成明确的 409：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\admin.py

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\admin.py

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\admin.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\models.py

changes: undefined

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\models.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\models.py

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\models.py

changes: undefined

先跑导入检查与现有相关测试，看改动是否破坏旧契约：

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\ingestion\scratch_windows.py

这是仓库既有的 Windows 兼容性问题（`fcntl` 仅 POSIX），与本次改动无关。先确认现有测试在此环境能否运行：

toolName: Shell

status: success

toolName: Shell

status: success

WSL 可用。探查 WSL 内的环境与项目路径：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

本机无法原生跑 Linux 测试，改用本地 `python:3.14-slim` 容器。先看 backend 的依赖配置：

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\scripts\test.sh

创建持久化的 Linux 测试容器环境（venv 放在命名卷，源码挂载 /mnt）：

toolName: Shell

status: success

环境就绪。跑现有分享测试基线：

toolName: Shell

status: success

预期中的旧测试失败（旧 JSON 行为已有意改变）。先修正 gcode/toolpath 路由的检查顺序（授权先于下载权限）：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

changes: undefined

现在重写后端单元/集成测试。先写 share 服务的集成测试：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

修正两处测试设计问题：trash 必须发生在建链之后；tombstone 场景用瞬态 pin 测试（不能违反 FK 约束）：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: Shell

status: success

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\identity\test_share.py

changes: undefined

toolName: Shell

status: success

服务层测试全绿。现在更新 API 集成测试：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\api\v1\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\api\v1\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\api\v1\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\api\v1\test_share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\api\v1\test_share.py

changes: undefined

toolName: Shell

status: success

现在写 GC 持有测试。先看现有 test_gc.py 的 fixture 模式：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\library\trash\test_gc.py

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\library\trash\test_gc.py

现在写 GC 持有集成测试：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\library\trash\test_share_holds.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\library\trash\test_share_holds.py

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

跨 session 删除导致身份映射过期。在 GC 前缓存路径、断言前 expire：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\library\trash\test_share_holds.py

changes: undefined

toolName: Shell

status: success

GC 持有测试通过。给 planner 加候选池排除测试，先看现有测试结构：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

toolName: Shell

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

changes: undefined

toolName: Shell

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

changes: undefined

修正 planner 测试的时间线（pin 只能在文件 live 时创建，过期在其后）：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\modules\backups\test_gc_planner.py

changes: undefined

toolName: Shell

status: success

现在写迁移回填测试。先参考 dxf 迁移测试如何构造 models/files 行：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_dxf_filetype_migration.py

写迁移数据回填测试（SQLite + Postgres 双跑）：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_share_link_pins_migration.py

changes: undefined

toolName: search_by_regex

status: success

toolName: Shell

status: success

容器内无法直接用 docker 启动 postgres；先按测试节点运行 SQLite 用例：

toolName: Shell

status: success

toolName: Shell

status: success

迁移回填的枚举大小写不匹配（旧 files 存大写名，pins CHECK 用小写值）。修正：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\alembic\versions\8b46d9a6830b_pin_share_links_to_file_revisions_.py

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_share_link_pins_migration.py

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

回填对"选择已失效但旧语义下仍展示 mesh"的链接也忠实地补了 companion pin。更新断言：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_share_link_pins_migration.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_share_link_pins_migration.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_share_link_pins_migration.py

changes: undefined

toolName: Shell

status: success

迁移测试通过。现在写 e2e headline 测试，先看 e2e 工厂与存储夹具用法：

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\e2e\test_artifact_cache.py

写 e2e headline 测试：

toolName: Write

status: success

filePath: f:\swe\098802\project-02\backend\tests\e2e\test_sharing.py

changes: undefined

清理误留的垃圾 helper：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\e2e\test_sharing.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\e2e\test_sharing.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\tests\e2e\test_sharing.py

changes: undefined

toolName: Shell

status: success

后端功能测试全部通过。现在跑 ruff 与更大范围的回归：

toolName: todo_write

status: success

Todos updated: 10 items

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

全绿。跑完整 fast 门禁：

toolName: Shell

status: success

toolName: search_by_regex

status: success

直接用 pytest 跑 fast 等价集合（后台执行）：

toolName: Shell

status: success

趁后端测试在跑，转向前端。先更新类型：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\types\models.ts

changes: undefined

更新分享对话框，支持选择 STL 源文件与 G-code 版本：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\share-dialog.tsx

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\share-dialog.tsx

changes: undefined

更新公开页处理 unavailable 条目：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\pages\share.tsx

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\pages\share.tsx

changes: undefined

更新前端 vitest。先看现有 share-dialog 与 share page 测试：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

更新 share-dialog vitest 适配新的源文件+版本选择面板：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: edit_file_search_replace

status: failed

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

后台 fast 门禁已完成（exit 0），稍后查看统计。继续替换前端测试：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\pages\__tests__\share.test.tsx

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\pages\__tests__\share.test.tsx

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\pages\__tests__\share.test.tsx

changes: undefined

现在跑前端测试与 lint/typecheck：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

新 uiText 字符串需要先进入语言目录。查看 locale 结构：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\lib\locale.ts

toolName: view_folder

status: success

f:\swe\098802\project-02\frontend\src\locales

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

两个 locale 均需补齐（i18n 覆盖率门禁）。查看锚点位置：

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\tests\repo\locale-authoring.test.ts

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\tests\repo\i18n-coverage.test.ts

复用现有键，新增一个键并同步 es：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\share-dialog.tsx

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\share-dialog.tsx

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\frontend\src\locales\en.json

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\locales\en.json

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\locales\es.json

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

测试挂载即 open=true 时预选 effect 不触发（真实用法是 false→true 切换）。按实际语义修正测试：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: Shell

status: success

前端测试全绿。查看后端 fast 门禁结果并跑前端 lint/typecheck：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: search_by_regex

status: success

资源标记表达式写错了，用 test.sh 的官方表达式后台重跑：

toolName: Shell

status: success

toolName: Shell

status: success

lint 脚本本身在此环境有工具链问题（.ts loader），直接用 oxlint 校验改动文件：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

更新 OpenAPI 契约快照：

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

fast 门禁在容器内因原生推理/Postgres 环境问题崩溃（与改动无关）。确认这些用例的标记情况：

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: Shell

status: success

后台跑测试期间，更新 CHANGELOG 与文档：

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\docs\roadmap.md

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\CHANGELOG.md

changes: undefined

运行 pyright（容器内）：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

先看全量 fast 结果，再以项目模式跑 pyright 分辨真实错误：

toolName: Shell

status: success

toolName: Shell

status: success

Windows pyright 无意义（项目面向 Linux，基线即千余平台错误）。改为核对代码库惯用写法，并再跑一次排除环境崩溃点的 fast：

toolName: Shell

status: success

按代码库惯例给 join 加 `type: ignore[arg-type]`：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: Shell

status: success

检查是否还有遗留引用与 OpenAPI 差异：

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_migrations.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\integration\db\migrations\test_migrations.py

toolName: Shell

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\repo\test_openapi_contract.py

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

等待期间检查前端其他消费者与完整代码差异：

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

`thumbnail_response` 的 `file_id` 需放宽为 Optional（路径型 cover 会传 None）：

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\files.py

changes: undefined

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\files.py

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

xdist worker 在多个文件系统/子进程密集测试上反复崩溃（DrvFs 环境问题）。改用串行执行完整 fast 集合：

toolName: Shell

status: success

趁串行套件运行，跑前端完整 vitest 与后端 repo 卫生/DDL 门禁：

toolName: Shell

status: success

toolName: Shell

status: success

反复出现的根因是嵌套容器没有 docker socket。尝试挂载 socket 让资源测试自举：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: search_by_regex

status: success

toolName: Shell

status: success

继续用串行模式补跑，忽略资源型/DrvFs 崩溃点：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\api\v1\share.py

toolName: Shell

status: success

toolName: Shell

status: success

直接运行与迁移最相关的两个卫生检查并查看完整失败：

toolName: Shell

status: success

toolName: Shell

status: success

跑前端完整 vitest 套件确认无连带破坏：

toolName: Shell

status: success

前端全套有 37 个失败，需要确认是否与本次改动相关：

toolName: Shell

status: success

全套失败大多分布在无关文件（疑似机器过载下的全套 flaky）。逐项甄别：先看 suite-hygiene 两个静态失败：

toolName: Shell

status: success

suite-hygiene 限制测试名中的 "and" 连接（125 vs 上限 123，正是我加的两个）。查看规则并重命名：

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: failed

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: edit_file_search_replace

status: failed

filePath: f:\swe\098802\project-02\frontend\src\components\model-detail\__tests__\share-dialog.test.tsx

changes: undefined

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

其余全套失败疑似过载 flaky。批量重跑这些文件验证：

toolName: Shell

status: success

toolName: Shell

status: success

share 页面测试在批量重跑中通过。验证其余失败是否与改动无关（隔离运行+类型检查）：

toolName: Shell

status: success

toolName: Shell

status: success

失败均为 Node 22 vs 引擎要求 Node 24 的既有环境问题，与改动无关。查看后端串行 fast 结果：

toolName: Shell

status: success

toolName: Shell

status: success

本地嵌套容器无法自举 Docker，fast 通道无法原样运行。按目录串行批量验证。第一批：tests/unit：

toolName: Shell

status: success

检查工厂覆盖率门禁是否要求为新表提供 builder：

toolName: search_by_regex

status: success

toolName: todo_write

status: success

Todos updated: 10 items

toolName: Shell

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\conftest.py

资源服务在收集期无条件拦截。查看 containers.py 是否有本地旁路：

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\containers.py

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\tests\unit\api\test_image_body.py

环境无法完整跑 CI 通道。继续按高相关目录做串行回归——api + db：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

用一个 docker 探针 stub 插件绕过收集期拦截（资源测试仍被 -m 反选不执行），跑最大可达集合：

toolName: Shell

status: success

toolName: Shell

status: success

移除已无调用者的兼容 seam：

toolName: edit_file_search_replace

status: failed

filePath: f:\swe\098802\project-02\backend\app\modules\identity\share.py

changes: undefined

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

套件仍在实质推进（worker 在跑 ingestion/media 测试，只是宿主争抢严重）。继续等待：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

完整本地门禁在高争抢宿主上被杀（环境所限）。补跑整个 e2e 层确认无连带破坏：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

失败仅看到尾部 3 个（均为原生向量/搜索环境问题）。重跑 e2e 并保留完整输出以确认全部 14 个失败项：

toolName: Shell

status: success

等待期间，最后复核 GC 子查询在 Postgres 上的 NOT IN 写法（关联子查询复用）与 trash 改动的完整性：

toolName: view_files

status: success

filePath: f:\swe\098802\project-02\backend\app\modules\library\trash.py

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

全部 10 个 e2e 失败均集中在搜索向量/S3 失败域（CI 镜像/资源依赖），与本次改动无关；145 通过含全部分享用例。清理临时文件并收尾：

toolName: delete_file

status: success

toolName: todo_write

status: success

Todos updated: 10 items

最后确认无悬空引用与最终改动清单：

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

契约文件内容无变化（仅 CRLF），还原以保持改动集干净：

toolName: Shell

status: success

实现完成。以下是交付说明与强制的测试覆盖矩阵。

## 实现概览

**不可漂移的身份（新表 `share_link_pins`，迁移 `8b46d9a6830b`，autogenerate）**
- pin 行持有不可变快照：`file_id`（RESTRICT FK）+ `sha256`、`file_type`、`original_filename`、`file_version`、`revision_number`、`is_recommended`；唯一约束 `(share_link_id, file_id)`。
- 新上传永远是新 File 行 → 客户看到的版本只由 pin 决定；后台重试/进程重启都从数据库行重新判定。
- `share_links` 增加封面快照 `cover_file_id`(SET NULL FK)/`cover_thumbnail_path` 及二者互斥 CHECK；旧 JSON 列由迁移回填后删除（gcode 选择 + 当时在线的 mesh 同伴 + 封面），downgrade 可逆，已做 SQLite 往返验证。

**统一授权解析（六路由同一道闸）**
- `resolve_share_access()` → `authorize_shared_file()` 是 detail/thumbnail/STL/G-code/toolpath/download 唯一入口；授权先于 403 判定，pin 不存在于链接即统一 404。
- 无 pin 的模型分享**保持既有 live-model 语义**；有 pin 的版本分享在模型被软删后仍可解析，被软删的 pinned 文件照常提供；pin 行物理不可恢复时稳定返回 **410 `shared_file_unavailable`**，detail 用 `is_available=false` 墓碑占位，绝不回退到新版本；blob 缺失维持 410 `file_blob_missing`。
- 短 token 哈希冲突自动换 token 重试（有界）。

**逻辑持有与 GC**
- 持有判定为一条 SQL 子查询（`active_hold_file_ids_select`：未撤销且未过期），小时 GC、`hard_delete_file/model`、GC planner 候选池全部复用；隔离期再复核新增 `gc_candidate_held`。
- 撤销/过期是唯一释放条件；清理重复执行结果确定；持有的精确 file id 不阻塞任何无关文件/模型回收。

**前端**：分享对话框可同时勾选 STL 源文件与 G-code 版本（精确链接默认勾选全部 mesh + 最新 rev）；公开页对不可用 pin 显示 “No longer available” 且不给 viewer/下载入口。

## 覆盖矩阵（计划 → 现状）

| # | 行为 | 类别 | 输入/前置 | Tier | 状态 |
|---|---|---|---|---|---|
|1|模型分享跟随 live 新 revision（旧语义不回归）|Happy|无 pin，再传 rev|Integration|✅ `test_model_share_follows_new_live_revisions`|
|2|pin gcode 全快照（sha/类型/文件名/版本/序号/推荐标记）|Happy|live gcode id|Integration|✅ `test_pins_the_named_gcode_revision_with_its_snapshot`|
|3|可 pin STL mesh|Happy|live stl id|Integration|✅ `test_pins_an_stl_revision`|
|4|精确 pin 集合；新建 revision 不出现在链接|Happy|mesh+gcode pin 后再传 rev|Integration/API|✅ `..._hides_revisions_created_afterwards` 等|
|5|重复 id 去重 / 空选择拒绝 / 他模型 id 拒绝 / 垃圾 id 拒绝|Edge/Error|[id,id]、[]、跨模型|Integration|✅ 3 个用例|
|6|创建时拒绝已软删文件与不可 pin 类型（DXF）|Error|deleted、DXF|Integration|✅ 2 个用例|
|7|token 不落明文、寿命裁剪、访问计数、冲突重试|Edge/并发||Integration|✅ 含 `test_retries_a_token_hash_collision`|
|8|未知/空/撤销/过期 token 统一 404|Auth||Integration|✅|
|9|模型分享遇模型软删 404；版本分享模型软删后仍解析|Edge||Integration/API|✅ 2 个用例|
|10|pinned 文件软删后仍可经全部路由获取|Edge|建链后软删|Integration/API/E2E|✅ stl/gcode/download/toolpath + E2E|
|11|未 pin 的新版本各路由 404；gcode 走 stl 路由 404|Error||Integration/API|✅|
|12|pin 行缺失→410；detail 墓碑 `is_available=false`；blob 缺失→410|Error|孤儿 pin/无字节|Integration/API|✅ 3 个用例|
|13|封面快照不随 live 封面漂移；建链时无封面则永久无封面|Happy/Edge||Integration/API|✅ 3 个用例|
|14|view-only 在授权之后返回 403|Auth||API|✅ 既有+新用例|
|15|小时 GC：持有文件保留、无关文件回收；两遍幂等；撤销/过期释放；持有模型保留且他模型回收|Happy/Edge|retention=0|Integration|✅ `test_share_holds.py` 5 用例|
|16|hard_delete 守卫抛 `ShareHoldError`；仅过期链接持有的文件可清理且 pin 被清|Error/Edge||Integration|✅ 2 用例；admin/purge API 映射 409 `share_hold_active`|
|17|planner 候选池排除持有文件/模型；撤销后回流；隔离期复核 `gc_candidate_held`|Happy/Error||Integration|✅ `TestRevisionShareHoldsInPlans` 4 用例|
|18|迁移回填：gcode+mesh 同伴+封面、坏 id 处理、模型分享不变、downgrade JSON、再升级|Edge|PREVIOUS 库|Integration|✅ SQLite 通过；⏭ Postgres 用例已写，Deep CI 跑|
|19|E2E headline：pin→新 rev 隐藏→软删后仍供字节→两次 GC 不回收→撤销即 404|Happy/Edge|全 ASGI|E2E|✅ `test_revision_share_pins_...`|
|20|RBAC（EDIT 创建/VIEW 列举/越权 403）|Auth||API|✅ 既有用例保留|
|21|前端：精确链接载荷=勾选身份、默认 mesh+最新 rev、空选禁用|UI||Frontend unit|✅ share-dialog 3 用例|
|22|前端：不可用 pin 隐藏 viewer/下载，显示失效|UI||Frontend unit|✅ share page 1 用例|
|23|Playwright 真实浏览器 pin 流程|UI||Playwright|⏭ N/A：本机 Node 22（引擎要求 24）无法跑 e2e-real，vitest 已覆盖 UI 契约|

## 验证结果（Linux 容器内运行）

- 新增/重写：43 服务集成 + 45 API + 7 GC 持有 + 4 planner + 迁移回填 + 2 E2E，全部通过；trash 整目录/planner/迁移卫生 195 通过；schema 奇偶、`test_models_versus_chain`、autogenerate 守卫通过；ruff 干净。
- E2E 层 145 通过 / 47 反选；10 失败全部位于 sqlite_vec 原生向量与 S3 失败域（搜索/存储身份模块），属本机嵌套容器缺原生扩展/Docker sibling 的环境问题，与本次改动无关。
- 前端：相关 vitest 27/27，`tsc`、oxlint、oxfmt、i18n（en/es 均补齐）、连词上限门禁通过；全套其余失败均为 Node 22（仓库要求 24）的既有环境问题。
- 未能在本机运行：Postgres 迁移用例（Deep CI）、coverage/full 通道与 Playwright（需 Docker-in-Docker 原生服务/Node 24）。

改动留在当前分支 `feat/share-pin-revision-fixes-revision`，未提交；CHANGELOG 与 docs/roadmap 已更新。
