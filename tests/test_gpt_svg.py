import io
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image

from app import gpt_svg


class SvgRenderingTest(unittest.TestCase):
    def svg(self, attributes='width="40" height="30"', content=''):
        return f'<svg xmlns="http://www.w3.org/2000/svg" {attributes}>{content}</svg>'.encode()

    def test_renders_vector_and_local_gradient_to_png(self):
        raw = self.svg(content='<defs><linearGradient id="red"><stop stop-color="red"/></linearGradient></defs>'
                       '<rect width="40" height="30" fill="url(#red)"/>')
        rendered = gpt_svg.render(raw)
        with Image.open(io.BytesIO(rendered)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (40, 30))
            self.assertEqual(image.convert("RGB").getpixel((20, 15)), (255, 0, 0))

    def test_dimensions_come_from_viewbox_or_absolute_units(self):
        for attributes, size in (('viewBox="0 0 40 30"', (40, 30)),
                                 ('viewBox="0 0 40 30" width="80"', (80, 60)),
                                 ('viewBox="0 0 40 30" height="60"', (80, 60)),
                                 ('width="1in" height="0.5in"', (96, 48)),
                                 ('width="100%" height="100%" viewBox="0 0 40 30"', (40, 30))):
            with self.subTest(attributes=attributes):
                with Image.open(io.BytesIO(gpt_svg.render(self.svg(attributes)))) as image:
                    self.assertEqual(image.size, size)

    def test_rejects_bad_xml_roots_entities_and_active_content_before_render(self):
        samples = [b'not xml', b'<html/>', b'<svg xmlns="https://example.com"/>',
                   b'<!DOCTYPE svg [<!ENTITY private SYSTEM "file:///secret">]><svg>&private;</svg>',
                   b'<?xml-stylesheet href="file:///secret.css"?><svg width="40" height="30"/>',
                   '<?xml-stylesheet href="https://example.com/a.css"?><svg/>'.encode('utf-16'),
                   self.svg(content='<script>alert(1)</script>'),
                   self.svg(content='<foreignObject><div>html</div></foreignObject>'),
                   self.svg(content='<rect onload="alert(1)"/>')]
        with patch("app.gpt_svg.subprocess.run") as run:
            for raw in samples:
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    gpt_svg.render(raw)
            run.assert_not_called()

    def test_rejects_external_references_in_attributes_and_css(self):
        bodies = ['<image href="file:///C:/secret.png"/>', '<image href="https://example.com/image.png"/>',
                  '<image href="//example.com/image.png"/>', '<image href="local.png"/>',
                  '<image href="data:image/svg+xml;base64,PHN2Zy8+"/>',
                  '<use xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="/secret.svg#id"/>',
                  '<rect fill="url(file:///secret)"/>', '<rect style="fill: url(https://example.com/a)"/>',
                  '<style>rect {fill: url(file:///secret</style>',
                  '<style>@import "https://example.com/a.css";</style>',
                  '<style>rect {fill: u/**/rl(https://example.com/a)}</style>',
                  '<style>rect {fill: u\\72l(https://example.com/a)}</style>']
        with patch("app.gpt_svg.subprocess.run") as run:
            for body in bodies:
                with self.subTest(body=body), self.assertRaises(ValueError):
                    gpt_svg.render(self.svg(content=body))
            with self.assertRaises(ValueError):
                gpt_svg.render(self.svg('width="40" height="30" xml:base="file:///secret/"', '<use href="#id"/>'))
            run.assert_not_called()

    def test_rejects_unbounded_dimensions_input_and_viewbox_before_render(self):
        attributes = ['width="8193" height="1"', 'width="5000" height="4001"',
                      'width="0" height="10"', 'width="-1" height="10"',
                      'width="NaN" height="10"', 'width="1e999" height="10"',
                      'width="1"', '', 'viewBox="0 0 0 30"', 'viewBox="0 0 40"',
                      'viewBox="0 0 inf 30"', 'width="100%" height="100%"',
                      'width="40" height="30" viewBox="0 0 -40 30"']
        with patch("app.gpt_svg.subprocess.run") as run:
            for value in attributes:
                with self.subTest(attributes=value), self.assertRaises(ValueError):
                    gpt_svg.render(self.svg(value))
            for raw in (b'', b'x' * (10 * 1024 * 1024 + 1)):
                with self.assertRaises(ValueError):
                    gpt_svg.render(raw)
            run.assert_not_called()

    def test_timeout_and_renderer_failures_are_bounded_errors(self):
        with patch("app.gpt_svg.subprocess.run", side_effect=subprocess.TimeoutExpired("render", 30)) as run:
            with self.assertRaisesRegex(ValueError, "30"):
                gpt_svg.render(self.svg())
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertNotIn("resources_dir", run.call_args.args[0])
        with patch("app.gpt_svg.subprocess.run", side_effect=OSError("missing")):
            with self.assertRaises(ValueError):
                gpt_svg.render(self.svg())
        for code, output in ((1, b''), (0, b'not PNG'), (0, b'x' * (10 * 1024 * 1024 + 1))):
            with self.subTest(code=code, size=len(output)):
                with patch("app.gpt_svg.subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout=output)):
                    with self.assertRaises(ValueError):
                        gpt_svg.render(self.svg())


if __name__ == "__main__":
    unittest.main()
