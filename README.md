# 配音中转站（nicevoice）

长文本 → 按字符数分割 → 多次调用 nicevoice 配音接口 → 合并为一个 mp3。

## 安装

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 使用流程（首次）

1. `python app.py` 启动 GUI
2. 点 **「登录保存 token」**（会弹出浏览器窗口，手动完成登录，含验证码/扫码也行），
   登录成功后 token 自动保存到 `token.json`
3. 点 **「抓包学习」**（强烈建议，一次性操作）：
   在弹出的页面里手动合成一次配音，工具会自动记录 `/clone2/tts` 的真实
   请求格式（URL / headers / body），并写入 `config.json` 作为请求模板
4. 回到 GUI：填 **音色ID**（网站上你克隆的音色）、粘贴长文本、点 **「开始生成」**

## 日常使用

直接粘贴文本 → 开始生成。token 过期时日志会提示，重新点一次「登录」即可。

## 配置文件 config.json

| 字段 | 说明 |
|---|---|
| `tts_url` | 配音接口地址 |
| `headers` / `body` | 请求模板，`{{token}}`/`{{text}}`/`{{audio_id}}` 为占位符 |
| `login_headless` | 登录是否用无头窗口（有验证码时建议 false） |
| `split_at_punctuation` | 分段时优先在标点处断句（避免把词切碎） |
| `request_interval` | 每段之间的间隔秒数 |

## 输出

合并后的 mp3 保存在 `output/配音_HHMMSS.mp3`。装有 ffmpeg 时用 ffmpeg 合并（音质衔接更稳），否则直接字节拼接。
