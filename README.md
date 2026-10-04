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
2. 点 **「登录保存 token」**（弹出浏览器窗口，手动完成登录，含验证码/扫码也行），
   登录成功后凭据自动保存到 `token.json`
3. 音色下拉框选好音色（启动时会自动拉取账号下的克隆音色列表）
4. 粘贴长文本、点 **「开始生成」**

## 日常使用

直接粘贴文本 → 开始生成。token 过期时日志会提示，重新点一次「登录」即可。

顶部「分割字符数」默认 20：超过该长度的文本会被切成多段分别合成，最后合并成一个 mp3。
分段会优先在标点处断句，避免把词切碎。

## 代理设置

**不需要手动配端口。** 程序启动时按以下顺序自动判断：

1. `config.json` 里的 `proxy` 字段（手动指定时优先）
2. macOS **系统代理**设置（`HTTPS` → `HTTP` → `SOCKS`，端口是多少就用多少）
3. 都没有 → 直连

所以只要 Clash / Surge 之类的代理把「系统代理」打开，无论端口是 7890、7897 还是别的，
程序都能自动跟随。换端口不用改代码。

需要手动指定时，在 `config.json` 里加：

```json
{
  "proxy": "127.0.0.1:7897"
}
```

- 填 `"none"` / `"direct"` / `"off"` → 强制直连（不走代理）
- 省略或留空 → 自动读系统代理
- 支持 `http://`、`socks5h://` 前缀，省略时按 `http://` 处理

生成的音频在 `output/配音_HHMMSS.mp3`（打包版为 `~/.voice-relay/output/`）。
登录态、配置等数据在源码运行时位于项目目录，打包版统一放在 `~/.voice-relay/`。
