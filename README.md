# Douyin ADB Control Skill

一个可移植的 [Agent Skill](https://agentskills.io/)，通过 Python 标准库控制器和 Android ADB，在已授权设备上执行有限、半自动的抖音操作。

它不是某个 Agent 客户端的专属插件。仓库根目录的 [`SKILL.md`](SKILL.md) 是 Agent 执行入口；[`scripts/douyin_control.py`](scripts/douyin_control.py) 也可以独立作为 JSON CLI 使用。

## 能力与边界

- 只读诊断：ADB、设备、抖音包、前台应用、屏幕尺寸和可选输入法状态。
- 浏览操作：截图、UI dump、打开/停止抖音、上滑或下滑信息流。
- 账号操作：点赞、关注、评论和任意点击均采用“准备摘要 → 新消息确认 → 再次截图核验 → 单次执行”的双回合协议。
- 不包含无人值守循环、批量互动、人脸/年龄/性别/颜值识别、旧仓库凭据、APK 或历史依赖。
- 只应操作你拥有或明确获准控制的 Android 设备，并遵守平台规则。

## 环境要求

- Python 3.9 或更高版本；没有需要 `pip install` 的 Python 依赖。
- [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)，并确保 `adb` 可执行文件在 `PATH` 中，或通过全局参数 `--adb /absolute/path/to/adb` 指定。
- 已开启 USB 调试并完成 ADB 授权的 Android 设备。
- 设备上已安装抖音；默认包名为 `com.ss.android.ugc.aweme`。
- 发送 UTF-8 评论时，需要用户自行安装、启用并选中兼容的 ADB Keyboard；本项目不会安装 APK 或切换输入法。

## 安装

Agent Skills 规范定义 Skill 内部结构，但不强制统一安装目录。为了跨客户端复用，推荐安装到项目级或用户级 `.agents/skills/`。如果你的 Agent 使用其他目录，请把整个仓库放到该客户端配置的 skills 目录中，并确保最终目录名为 `douyin-adb-control`。

### 项目级安装（推荐）

在需要使用该能力的项目根目录执行：

```sh
mkdir -p .agents/skills
git clone https://github.com/hayecheng/douyin-adb-control-skill.git .agents/skills/douyin-adb-control
```

### 用户级安装（macOS / Linux）

```sh
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/hayecheng/douyin-adb-control-skill.git "$HOME/.agents/skills/douyin-adb-control"
```

### 用户级安装（Windows PowerShell）

```powershell
$SkillsDir = Join-Path $HOME ".agents\skills"
$null = New-Item -ItemType Directory -Force -Path $SkillsDir
git clone https://github.com/hayecheng/douyin-adb-control-skill.git (Join-Path $SkillsDir "douyin-adb-control")
```

安装后重启或重新载入 Agent 会话，让客户端重新扫描 `SKILL.md`。客户端若不扫描 `.agents/skills/`，请改用它自己的 skills 目录或配置项。

## 安装验证

`doctor` 是只读命令，不会触发设备点击或账号变更。

macOS / Linux：

```sh
SKILL_DIR="$HOME/.agents/skills/douyin-adb-control"
python3 "$SKILL_DIR/scripts/douyin_control.py" --json doctor
```

Windows PowerShell：

```powershell
$SkillDir = Join-Path $HOME ".agents\skills\douyin-adb-control"
py -3 "$SkillDir\scripts\douyin_control.py" --json doctor
```

成功调用 CLI 时顶层会返回 `"ok": true`。仍需检查 `data.healthy`：只有 `data.healthy: true` 才表示 ADB、设备和目标应用等必要检查全部通过。`doctor` 在环境不完整时也可能以退出码 `0` 返回结构化诊断结果。

## 首次只读检查

全局参数必须放在子命令之前。先列出设备，选择状态为 `device` 的序列号，再固定该设备执行后续命令：

```sh
python3 "$SKILL_DIR/scripts/douyin_control.py" --json devices

SERIAL="replace-with-device-serial"
python3 "$SKILL_DIR/scripts/douyin_control.py" --serial "$SERIAL" --json status
python3 "$SKILL_DIR/scripts/douyin_control.py" --serial "$SERIAL" --json screenshot --output "/tmp/douyin-current.png"
```

检查截图并确认当前界面后，再让 Agent 执行导航或准备账号操作。不要依据默认坐标、旧截图或文件名猜测当前目标。

## 可选配置

默认配置位于 [`assets/config.example.json`](assets/config.example.json)。建议复制到 Skill 目录之外再修改，避免更新仓库时产生冲突：

```sh
CONFIG_DIR="$HOME/.config/douyin-adb-control"
mkdir -p "$CONFIG_DIR"
cp "$SKILL_DIR/assets/config.example.json" "$CONFIG_DIR/config.json"

python3 "$SKILL_DIR/scripts/douyin_control.py" \
  --config "$CONFIG_DIR/config.json" \
  --json doctor
```

示例中的坐标和滑动参数只是起始配置，不代表当前 UI。任何坐标操作都必须基于新截图和当前屏幕尺寸重新核验。

## 让 Agent 使用

安装后，可以用类似请求触发该 Skill：

```text
检查已连接的 Android 设备和抖音运行状态，只执行只读诊断。
```

```text
基于新截图准备一次点赞操作，展示完整确认摘要；在我下一条消息明确批准前不要执行。
```

对于点赞、关注、评论和任意点击，用户最初的“直接执行”不构成确认。Agent 必须先运行 `prepare-action`、展示具体摘要并结束当前回合；只有看到摘要后的新消息才能批准执行。详细协议见 [`SKILL.md`](SKILL.md) 和 [`references/safety.md`](references/safety.md)。

## 更新

```sh
git -C "$SKILL_DIR" pull --ff-only
python3 "$SKILL_DIR/scripts/douyin_control.py" --json doctor
```

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
```

当前测试集覆盖控制器核心、CLI JSON 契约、双回合令牌、隐私通道、并发配额、超时不确定状态和 Windows/POSIX 文件行为。命令参考见 [`references/commands.md`](references/commands.md)。

## License

[MIT](LICENSE)。本项目是从零实现，灵感来自 MIT 许可的 [wangshub/Douyin-Bot](https://github.com/wangshub/Douyin-Bot)，未复用其凭据、APK、人脸数据、依赖或自动化实现。
