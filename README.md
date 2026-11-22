<div align="center">

<img src="https://cdn.jsdelivr.net/gh/HoshimiSenya/astrbot_plugin_task_list@main/logo.png" width="120" alt="任务列表 Logo">

# 任务列表

**读取 AstrBot 的未来定时任务，分区渲染为图片**

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.5-5865f2?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-3b82f6?style=flat-square)](LICENSE)

</div>

![渲染效果](https://cdn.jsdelivr.net/gh/HoshimiSenya/astrbot_plugin_task_list@main/docs/preview.png)

## ✨ 特性

- 🗂 **分区展示**：按「一次性提醒」「循环提醒」分区排列，按下次执行时间升序
- 🏷 **来源标注**：标注 AI 自建 / 用户自建 / 插件内置 / 插件自建
- 📊 **双模输出**：默认渲染图片，可切换纯文本，渲染失败自动回退
- ⚡ **指纹缓存**：内容未变化时直接复用上次渲染的图片
- 🌏 **时区换算**：展示时间按配置时区换算，差异时自动标注
- 🛡 **权限管控**：默认仅管理员可用，可开放给所有用户

## 🏷 来源判定

| 标签 | 判定依据 | 说明 |
| --- | --- | --- |
| 插件内置 | `job_type == "basic"` | 插件静态注册的回调任务 |
| AI 自建 | `payload["origin"] == "tool"` | 内置 `future_task` 工具创建 |
| 用户自建 | `payload["origin"] == "api"` | WebUI 面板创建 |
| 插件自建 | payload 中无 `origin` | 插件调用 `add_active_job` 且未标记来源 |

`origin` 自 cron 功能引入（v4.14.0）即存在，AstrBot 自身创建的任务均可判定。第三方插件如需被正确识别，可在 payload 中写入 `"origin": "plugin"`。

## 🚀 安装

### 方式一：插件市场（推荐）

在 AstrBot 管理面板 → **插件** → **插件市场**，搜索 **“任务列表”** 并安装。

### 方式二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/HoshimiSenya/astrbot_plugin_task_list
pip install -r astrbot_plugin_task_list/requirements.txt
```

重启 AstrBot，然后在管理面板启用插件。

> 插件要求 **AstrBot 4.5 及以上（4.x 系列）**。  
> 图片渲染依赖 Pillow（AstrBot 自带）；Windows 环境额外需要 `tzdata`（`requirements.txt` 已包含）；服务器需安装中文字体（如 `fonts-noto-cjk`），否则无法出图。

## 💬 使用

在聊天中发送：

```text
/任务列表
```

插件默认仅管理员可用；如需开放给所有用户，在管理面板中将「仅管理员可用」关闭。

## ⚙️ 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `output_mode` | string | `image` | 输出模式：image / text |
| `image_cache` | bool | `true` | 内容未变化时复用上次渲染的图片 |
| `cache_max_mb` | int | 32 | 缓存容量上限，按最久未使用淘汰；0 为不限制 |
| `cache_ttl_hours` | int | 168 | 缓存有效期；0 为不过期 |
| `admin_only` | bool | `true` | 仅管理员可用 |
| `only_current_session` | bool | `false` | 仅显示当前会话的任务 |
| `show_session` | bool | `false` | 显示任务所属会话 |
| `show_description` | bool | `true` | 显示任务备注 |
| `timezone` | string | 空 | 展示用时区（IANA 名称），留空跟随系统 |
| `render_scale` | int | 2 | 图片超采样倍数，1–4 |

## 📝 说明

- **下次执行时间**优先从 APScheduler 读取实时值；数据库副本为异步回写，可能滞后
- **时区**：cron 表达式按任务自身时区解释，展示时间按配置时区换算；不一致时在重复规则后标注任务时区
- **缓存指纹**由任务数据、渲染参数、样式版本与字体路径共同决定，任一变化即重新渲染

## 🧪 测试

```bash
python tests/test_task_list.py     # 纯逻辑单元测试，无需 AstrBot 环境
python tests/test_end_to_end.py    # 端到端测试，使用最小框架桩
```

## 📄 许可证

[MIT](LICENSE)
