# WebUI 文件管理器示例插件

这是一个基于 [GUGUWebUI](https://github.com/PFingan-Code/PF-MCDR-WebUI) 的文件管理器插件：

- 浏览/预览：MCDR 根目录（`scope=mcdr`）
- 下载：支持文件下载
- 编辑/上传/删除/重命名/创建目录：
  - 仅超级管理员可用
  - 超级管理员额外可对系统本地磁盘使用 `scope=drive`

## 安装

1. 将 `examples/webui_file_manager/` 整个目录复制到 MCDR 的 `plugins/` 下：
   - `plugins/webui_file_manager/mcdreforged.plugin.json`
   - `plugins/webui_file_manager/webui_file_manager.py`
   - `plugins/webui_file_manager/static/demo.html`
2. 确保已启用 `guguwebui`（PF-MCDR-WebUI）。
3. 重载插件或重启 MCDR：
   - `!!MCDR plugin reload webui_file_manager`

## 使用

1. 登录 WebUI
2. 左侧边栏展开「插件网页」
3. 打开 `webui_file_manager` 页面
4. 选择 Scope：
   - 非超管：只能看到 `mcdr`
   - 超管：可看到 `mcdr` + 多个 `drive`（例如 `C:\`）

## API 约定（插件端）

所有接口均通过 WebUI 框架代理：

- `GET /api/plugin/webui_file_manager/fs_info`
- `GET /api/plugin/webui_file_manager/list?scope=mcdr|drive&path=...`
- `GET /api/plugin/webui_file_manager/read?scope=...&path=...`
- `GET /api/plugin/webui_file_manager/download?scope=...&path=...`
- `POST /api/plugin/webui_file_manager/save?scope=...&path=...`（JSON：`{content}`）
- `POST /api/plugin/webui_file_manager/upload?scope=...&path=...`（multipart：字段名 `file`，`path` 表示目标目录）
- `POST /api/plugin/webui_file_manager/delete?scope=...&path=...`（JSON：`{path, recursive}`）
- `POST /api/plugin/webui_file_manager/rename?scope=...&path=...`（JSON：`{path, new_name}`）
- `POST /api/plugin/webui_file_manager/mkdir?scope=...&path=...`（JSON：`{path}`）

其中 `params.auth` 已由框架注入插件 `api_handler` 的 `params` 参数里（见 `docs/WebApi.md` 中 `params.auth` 说明）。

## 安全说明

- 前端传入的 `path` 只允许相对于 scope base 的相对路径；`..` 与绝对路径会被拒绝
- 后端会校验真实解析后的路径仍落在允许的 base 内
- 非超管禁止写操作
- 写操作/跨盘访问必须 `params.auth.is_super_admin == true`

## 页面 HTML 存放位置

本插件的页面 HTML 会在 `on_load` 时自动拷贝到：

- `./config/webui_file_manager/demo.html`