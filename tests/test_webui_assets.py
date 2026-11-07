# -*- encoding: utf-8 -*-
"""视觉资源与品牌回归；由 tools/run_webui_tests.py 在包导入前隔离状态。"""

import base64
import json
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImageSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.images.append(dict(attrs))


def webp_chunks(data):
    cursor = 12
    names = []
    while cursor + 8 <= len(data):
        name = data[cursor:cursor + 4]
        size = int.from_bytes(data[cursor + 4:cursor + 8], 'little')
        names.append(name)
        cursor += 8 + size + size % 2
    return names


class WebUIAssetsTest(unittest.TestCase):
    def test_display_name_is_geyi_and_namespace_is_unchanged(self):
        raw = (ROOT / 'app.json').read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        manifest = json.loads(raw)
        self.assertEqual(manifest['name'], '歌伊')
        self.assertEqual(manifest['namespace'], 'newGeYi')
        self.assertEqual(manifest['webui_config'][0]['path'], 'webui/index.html')

    def test_page_and_public_docs_drop_old_brand(self):
        # 不碰回滚目录；仅检查当前交付文字。
        forbidden = '歌伊' + '圣堂'
        files = [ROOT / 'webui/index.html', ROOT / 'README.md', ROOT / 'app.json']
        files.extend((ROOT / 'docs').glob('WEBUI*.md'))
        for path in files:
            self.assertNotIn(forbidden, path.read_text(encoding='utf-8'), path.name)

    def test_character_images_are_embedded_with_matching_webp_format(self):
        parser = ImageSources()
        parser.feed((ROOT / 'webui/index.html').read_text(encoding='utf-8'))
        images = [image for image in parser.images if image.get('src', '').startswith('data:image/webp;base64,')]
        self.assertGreaterEqual(len(images), 2, '头像和人物展示应随单页内嵌')
        for image in images:
            data = base64.b64decode(image['src'].split(',', 1)[1], validate=True)
            self.assertEqual(data[:4], b'RIFF')
            self.assertEqual(data[8:12], b'WEBP')
            self.assertEqual(int.from_bytes(data[4:8], 'little') + 8, len(data))
            self.assertNotIn(b'EXIF', webp_chunks(data))
            self.assertNotIn(b'XMP ', webp_chunks(data))
            self.assertLess(len(data), 220000, '素材应轻量，避免原图打包进管理页')
            self.assertIn('alt', image)

    def test_derived_assets_are_pixel_only_not_original_source_copies(self):
        for name in ('geyi-portrait.webp', 'geyi-avatar.webp'):
            data = (ROOT / 'webui/assets' / name).read_bytes()
            self.assertEqual(data[:4], b'RIFF')
            self.assertEqual(data[8:12], b'WEBP')
            self.assertNotIn(b'EXIF', webp_chunks(data))
            self.assertNotIn(b'XMP ', webp_chunks(data))
            self.assertLess(len(data), 220000)


if __name__ == '__main__':
    unittest.main()
