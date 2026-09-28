# PrivateBot 更新日志

## 2026-09-28 NapCat 登录与稳定性修复

- NapCat 一键登录脚本 C:\NapCat\Shell\start-napcat.bat
- 关闭 o3HookMode 降低掉线率
- 端口从 8080 改为 8081

## Stage 8 - 多模态识图能力

- config.py: +4 VISION_* configs
- adapter/onebot.py: extract_private_content()
- core/llm.py: chat_with_images()
- core/handler.py: image helpers + branch
- docs/stage8-design.md