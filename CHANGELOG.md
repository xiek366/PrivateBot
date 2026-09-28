# PrivateBot 更新日志

---

## 🗂️ 2026-09-28 NapCat 登录与稳定性修复

**属于 PrivateBot 项目运维配置，代码不在本仓库内。**

### 问题
- NapCat 启动后自动弹出二维码，用户无法扫码（无手机）
- 缓存登录态和密码登录均被腾讯风控拦截，强制要求验证
- NapCat 时不时把 QQ 踢下线（o3Hook 包拦截模块兼容性问题）
- PrivateBot 端口默认 8080 被 VS Code 占用

### 修复内容

#### 1. NapCat 一键登录脚本 C:\NapCat\Shell\start-napcat.bat
```
双击运行 → 自动注入 NAPCAT_QUICK_ACCOUNT + NAPCAT_QUICK_PASSWORD 环境变量
       → 调 launcher-user.bat -q 3777628209 启动
       → 退出后自动清掉密码变量
```
- 首次使用需手机配合扫码 + 短信验证一次（腾讯安全策略，无法绕过）
- 验证后登录态缓存在 NapCat 本地，以后直接密码登录

#### 2. NapCat WebUI 反检测配置
访问 http://127.0.0.1:6099/webui?token=aa9bd6249a22 → 反检测开关配置 页面：
- ✅ 关闭 o3HookMode（O3 Hook 模式）——降低掉线率的核心措施
- ❌ 不关掉上面 6 个小开关（Hook/Window/Module/Process/Container/JS），o3Hook 关掉后它们自然失效

#### 3. PrivateBot 端口调整
- .env 中 ONEBOT_WS_PORT=8081（原 8080 被 VS Code 占用）
- NapCat WebUI 同步改：反向 WS → ws://127.0.0.1:8081/onebot
- ONEBOT_ACCESS_TOKEN 留空（双方鉴权一致即可，内网无安全压力）

### 验收
- PrivateBot 启动：listening on ws://127.0.0.1:8081/onebot ✅
- NapCat 连接：client connected role=Universal self_id=3777628209 ✅
- 白名单消息收发正常 ✅

---

## 🎨 Stage 8 — 多模态识图能力

**commit: feat: stage 8 - multimodal image understanding**

### 新增能力
用户发图片（可配文字），机器人能看图描述 + 结合角色语气回复。

测试用例（已全部通过）：
| 场景 | 回复示例 |
|------|--------|
| 用户发一张结灯笑着的照片 | 我、我才没有笑得这么开心……别一直盯着看啦…… |
| 用户发一张画作（人物像结灯） | 画里的人长得有点像我，但绝对不是我。 |
| 只发图不写字 | 结灯主动描述图片内容 |

### 技术方案
- **模型**：deepseek-flash 原生支持多模态（2026.09.10 V4.1 Flash 起内置视觉编码器），无需换模型
- **图片来源**：优先 NapCat 提供的 CDN url（multimedia.nt.qq.com.cn），没有就读本地临时文件路径
- **大小上限**：单张 16MB（可配置），每次最多 3 张
- **画质**：detail=low（缩至 512×512），对聊天图片够用又省 token
- **历史存储**：session.json 里只存 [图片] 占位符，不存 base64（防文件膨胀）

### 改动文件
| 文件 | 改动 |
|------|------|
| config.py | +4 个配置项：VISION_ENABLED / VISION_MAX_IMAGES / VISION_MAX_MB / VISION_DETAIL |
| adapter/onebot.py | 新增 extract_private_content() 同时抽文字 + 图片 segments；旧 extract_private_text() 转发保持兼容 |
| core/llm.py | 新增 chat_with_images() —— content 从纯 string 改成 block 数组 |
| core/handler.py | 新增图片辅助函数（httpx 下载 / 本地读取 / base64 转码）+ _generate_reply 里加图片处理分支 |
| docs/stage8-design.md | 设计文档 |

### 新增配置（.env，全可选）
```env
VISION_ENABLED=true
VISION_MAX_IMAGES=3
VISION_MAX_MB=16
VISION_DETAIL=low
```

### 不做的事
- ❌ 不做 OCR 专用优化（让 LLM 自己处理）
- ❌ 不支持群聊图片（只限私聊）
- ❌ 机器人不发图片回复
- ❌ 图片不进长期记忆库（记忆提取只基于文本）

### 零新依赖
httpx（已有）、base64/mimetypes（标准库）。

### 可一键关闭
```env
VISION_ENABLED=false
```
关闭后回退到纯文本模式，行为与 Stage 7 完全一致。

### 未来方向
- 图片内容进长期记忆（如用户发了一张猫的照片 → preference）
- 多图上下文连贯（连续发多张时都传给 LLM）
- 图片回复能力（结灯可以回图片）
