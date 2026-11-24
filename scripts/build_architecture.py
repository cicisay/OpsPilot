"""Generate the architecture diagram."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets" / "architecture.png"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"

W, H = 1800, 1000
BG = "#F4F7FB"
INK = "#0F172A"
MUTED = "#64748B"
BLUE = "#2563EB"
BLUE_BG = "#EAF1FF"
GREEN = "#0F9F74"
GREEN_BG = "#E7F8F1"
AMBER = "#B45309"
AMBER_BG = "#FFF4E5"
PURPLE = "#6D4AFF"
PURPLE_BG = "#F0ECFF"
WHITE = "#FFFFFF"
LINE = "#CBD5E1"


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def wrap(draw, text: str, fnt, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + char
        if draw.textlength(trial, font=fnt) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def box(draw, xy, title, body, accent, bg, title_size=24, body_size=17, radius=18):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=bg, outline=accent, width=2)
    draw.rounded_rectangle((x1, y1, x1 + 8, y2), radius=4, fill=accent)
    tf = font(title_size, True)
    bf = font(body_size)
    draw.text((x1 + 24, y1 + 18), title, font=tf, fill=INK)
    y = y1 + 18 + title_size + 12
    for line in wrap(draw, body, bf, x2 - x1 - 44):
        draw.text((x1 + 24, y), line, font=bf, fill=MUTED)
        y += body_size + 6


def arrow(draw, start, end, color=LINE, width=3):
    draw.line([start, end], fill=color, width=width)
    x1, y1 = start
    x2, y2 = end
    import math

    angle = math.atan2(y2 - y1, x2 - x1)
    size = 12
    left = (x2 - size * math.cos(angle - math.pi / 6), y2 - size * math.sin(angle - math.pi / 6))
    right = (x2 - size * math.cos(angle + math.pi / 6), y2 - size * math.sin(angle + math.pi / 6))
    draw.polygon([end, left, right], fill=color)


image = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(image)

draw.text((60, 44), "OpsPilot / 跨境商品运营 Agent", font=font(38, True), fill=INK)
draw.text((60, 98), "四角色闭环：主管 → 工坊 → 合规 → 发布；店铺前台零改动", font=font(20), fill=MUTED)

# site
box(
    draw,
    (60, 180, 390, 400),
    "真实店铺",
    "Next.js 15 + Prisma + SQLite\nAgent API v1.1\nBearer token / idempotency\n店铺前台不加任何 UI",
    BLUE,
    BLUE_BG,
)
# connector
box(
    draw,
    (450, 200, 730, 380),
    "连接器",
    "SitePlatform（HTTP）\nSnapshotSite（离线预览）\ndry-run / 版本 / 回滚",
    GREEN,
    GREEN_BG,
)
# ops
box(
    draw,
    (60, 560, 390, 850),
    "OpsPilot 运营台",
    "登录 / 租户 / 角色\n任务单与方案 before-after\n一次性审批令牌\n审计：SQLite + CSV",
    PURPLE,
    PURPLE_BG,
)
# agent core
draw.rounded_rectangle((780, 140, 1720, 600), radius=24, fill=WHITE, outline=LINE, width=2)
draw.text((810, 165), "Agent 核心", font=font(26, True), fill=INK)
draw.text((810, 203), "硬规则：最多 2 轮返工 · 合规一票否决 · 发布令牌只发给主管", font=font(16), fill=MUTED)

roles = [
    ("运营主管", "体检 / 选品\n类目中位数\n30 → 5 个 TaskOrder", BLUE),
    ("内容工坊", "2–3 版方案\n文案 / 价格 / 素材\n3 包 × 3 版", GREEN),
    ("合规审查", "独立角色\n词库 + 长度\n9/9 通过", AMBER),
    ("发布编排", "dry-run → 令牌\n商品锁 / 幂等\n真实写入 0", PURPLE),
]
for index, (title, body, accent) in enumerate(roles):
    x = 810 + index * 220
    box(draw, (x, 245, x + 200, 470), title, body, accent, "#FFFFFF", title_size=20, body_size=14)
    if index < len(roles) - 1:
        arrow(draw, (x + 200, 350), (x + 220, 350), color=LINE, width=3)

draw.text((810, 500), "输入：真实指标 / 快照    输出：TaskOrder / 方案 / Review / 发布结果", font=font(16), fill=MUTED)
draw.rounded_rectangle((810, 530, 1690, 570), radius=10, fill="#F1F5F9")
draw.text((828, 539), "规则先行，指标可解释；LLM planner 是可替换层，不改变输出结构", font=font(16), fill=INK)

# data
draw.rounded_rectangle((780, 640, 1720, 860), radius=24, fill=WHITE, outline=LINE, width=2)
draw.text((810, 665), "数据与可观测", font=font(24, True), fill=INK)
data_items = [
    ("JSON 产物", "task_orders / proposals\nreview_results / publish_results"),
    ("SQLite", "users / user_tenants / tenants\naudit（按 tenant_id）"),
    ("安全", "PBKDF2 / CSRF / 租户隔离\n幂等键 / 商品锁 / 回滚"),
]
for index, (title, body) in enumerate(data_items):
    x = 810 + index * 295
    draw.rounded_rectangle((x, 715, x + 275, 835), radius=14, fill="#F8FAFC", outline=LINE)
    draw.text((x + 16, 728), title, font=font(18, True), fill=INK)
    y = 760
    for line in body.splitlines():
        draw.text((x + 16, y), line, font=font(14), fill=MUTED)
        y += 22

# arrows
arrow(draw, (390, 290), (450, 290), color=BLUE, width=4)
arrow(draw, (450, 320), (390, 320), color=BLUE, width=4)
arrow(draw, (730, 290), (780, 290), color=GREEN, width=4)
arrow(draw, (390, 700), (780, 700), color=PURPLE, width=4)
arrow(draw, (390, 760), (780, 760), color=PURPLE, width=4)
arrow(draw, (960, 600), (960, 640), color=LINE, width=3)

draw.text((60, 910), "阅读顺序：业务问题 → 架构 → 一次闭环 → 3 个难点 → 测试与边界 → 下一步", font=font(18, True), fill=INK)
draw.text((60, 946), "真实店铺只做 dry-run；演示模式用快照预览，发布明确拒绝。", font=font(16), fill=MUTED)

OUT.parent.mkdir(parents=True, exist_ok=True)
image.save(OUT)
print("saved:", OUT)
