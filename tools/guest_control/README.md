# Windows 采集节点受控发布（v2）

本目录替代历史 `guest/agent.ps1` 的任意命令执行通道。密钥和实际命令信封都属于运行时私有数据，禁止提交。

协议：宿主为每台节点生成 `control-<device_id>.json`，其中 `payload_b64` 是 UTF-8 JSON，`signature_b64` 是该原始 payload 的 HMAC-SHA256。payload 必含 `device_id`、`sequence`、`issued_at`、`expires_at`、`action`、`args`。agent 拒绝签名错误、身份不匹配、过期和重放的消息。

首次安装必须通过目标 VM 的虚拟控制台写入 `C:\ProgramData\WxSearchControl\agent.json`，其中仅有本机 `device_id` 和唯一 `hmac_key_b64`。不得用旧 HTTP 广播 agent 下发这个文件。

支持动作：`status`、`stop_after_current`、`stage_release`、`activate_release`、`rollback`。不支持任意 PowerShell 或 shell 文本。
