# -*- encoding: utf-8 -*-
"""独立入口：先隔离运行目录，再发现测试，避免包导入先建生产 Runtime。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    scratch = Path(tempfile.mkdtemp(prefix='geyi_webui_tests_'))
    # 无条件覆盖；不要继承开发 shell 中可能指向生产数据的 GEYI_DATA_DIR。
    os.environ['GEYI_DATA_DIR'] = str(scratch)
    os.environ['GEYI_API_KEY'] = ''
    os.environ['GEYI_OWNER_IDS'] = ''
    for key in tuple(os.environ):
        if key.startswith('GEYI_KEY_'):
            os.environ[key] = ''
    (scratch / 'config.json').write_text('{}', encoding='utf-8')
    sys.path.insert(0, str(root.parent))
    print('Isolated test state:', scratch, flush=True)
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), top_level_dir=str(root.parent))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('Temporary test state retained for diagnostics:', scratch)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
