"""扫描按钮的前端接线测试。

不跑浏览器，但把最容易断的几处钉死：HTML 里的 id、JS 里取的 id、
以及 JS 调的两个接口。改版时漏一个 id，页面就是点了没反应。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "ops_agent" / "console" / "static"

# 这些由 app.js 动态渲染，本来就不在 index.html 里：详情面板的四个按钮，
# 以及体检卡里"展开全文"的正文与按钮（随 checkup 内容有无而变化）。
DYNAMIC_IDS = {
    "approval-token-input",
    "approve-button",
    "preview-button",
    "publish-button",
    "health-para",
    "health-para-toggle",
}


class ScanUiWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.js = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "styles.css").read_text(encoding="utf-8")

    def test_html_has_every_scan_control(self):
        for element_id in (
            "scan-box",
            "scan-button",
            "scan-btn-label",
            "scan-btn-sub",
            "scan-steps",
            "scan-confirm",
            "scan-confirm-text",
            "scan-force",
            "scan-cancel",
            "scan-result",
            "scan-fresh",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"缺 id={element_id}")

    def test_js_only_touches_ids_that_exist(self):
        ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', self.js))
        missing = sorted(
            element for element in ids
            if element not in DYNAMIC_IDS and f'id="{element}"' not in self.html
        )
        self.assertEqual(missing, [], f"JS 取了不存在的元素：{missing}")

    def test_js_calls_both_scan_endpoints(self):
        self.assertIn("/scan/status", self.js)
        self.assertRegex(self.js, r"/scan`,\s*\"POST\"")
        self.assertIn('"POST"', self.js)

    def test_csrf_header_is_sent_on_post(self):
        # 扫描是写操作，漏了 CSRF 头会被服务端 403 挡下。
        self.assertIn('headers["X-CSRF-Token"] = state.csrf', self.js)

    def test_button_can_render_three_states(self):
        for token in ("is-busy", "扫描中", "重新扫描"):
            self.assertIn(token, self.js)
        for selector in (".scan-btn.is-busy", ".scan-steps li.done",
                         ".scan-steps li.failed", ".scan-steps li.running"):
            self.assertIn(selector, self.css)

    def test_unpublished_proposals_ask_before_overwriting(self):
        self.assertIn("scanPending", self.js)
        self.assertIn("unpublished", self.js)
        self.assertIn("仍然扫描", self.html)

    def test_demo_mode_hides_the_button(self):
        self.assertIn("overview.demo", self.js)
        self.assertIn("setHidden(button", self.js)

    def test_visibility_uses_the_hidden_class_not_the_attribute(self):
        # button / ol 都设了 display，只用 hidden 属性会被样式表盖掉，
        # 结果"隐藏"的元素还在屏幕上。统一走 .hidden 类。
        offenders = [
            line.strip() for line in self.js.splitlines()
            if ".hidden =" in line or "hidden = true" in line or "hidden = false" in line
        ]
        self.assertEqual(offenders, [], f"这些地方还在直接写 hidden 属性：{offenders}")
        self.assertIn("function setHidden(node, hidden)", self.js)
        self.assertIn(".hidden {\n  display: none !important;", self.css)

    def test_no_inline_script_in_html(self):
        # 页面靠 /static/app.js，别把逻辑塞回 HTML 里，否则 CSP 一上就哑。
        self.assertNotIn("<script>", self.html)


if __name__ == "__main__":
    unittest.main()
