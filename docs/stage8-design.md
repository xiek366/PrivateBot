# Stage 8 设计文档：识图能力（多模态图片理解）

## 一、目标

用户给机器人发图片，机器人能看图 + 结合用户文字提问，用角色语气回复。

## 二、范围边界

做什么：
1. 识别 OneBot v11 消息里的 image segment
2. 下载图片（优先 url，没有的话 NapCat 本地路径），转 base64
3. 把 base64 图片作为 image_url block 传给 deepseek-flash
4. 历史对话里上一轮有图片也一并传给 LLM

不做什么：
- 不做 OCR 专用优化
- 不保存图片到磁盘
- 不支持群聊图片
- 不做图片回复
- 图片不进长期记忆库

## 三、技术方案

图片来源优先级：url > 本地路径 > 兜底
大小限制：单张 16MB，每次最多 3 张，detail=low

## 四、文件清单

- adapter/onebot.py: 新增 extract_private_content()
- core/llm.py: 新增 chat_with_images()
- core/handler.py: 图片下载/base64 + 处理分支
- config.py: +4 VISION_* 配置
- docs/stage8-design.md: 本文件

## 五、验收

- 发图片 + 文字 -> 机器人能看图回复
- 只发图片 -> 主动描述
- session.json 存 [图片] 而非 base64
- VISION_ENABLED=false 回退纯文本

## 六、风险

- 图片 token 比文字贵
- NapCat 本地路径版本间不一致
- base64 膨胀约 33%
