"""从同一份参数中文注释生成维护文档，不连接数据库或设备。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.config_metadata import COLUMN_COMMENTS, PARAMETER_DESCRIPTIONS, FIELD_DESCRIPTIONS


def export(path: Path):
    lines = ['# config表中文参数说明', '',
        '本说明与数据库description、配置接口fieldDescriptions共用同一份中文目录。', '',
        '数据库中的value是JSON，不能直接写入//或/* */注释。各参数的说明保存在description，嵌套字段也逐项列在其中；表和列的中文注释可在数据库客户端查看。', '',
        '参数值通过配置接口保存后重启后端整批生效。更新说明本身不会修改参数值、revision或updated_by。说明中“兼容保留”不代表当前检测流程正在使用该参数。', '',
        '## 数据表字段', '', '| 字段 | 中文说明 |', '|---|---|']
    lines.extend(f'| `{key}` | {note} |' for key, note in COLUMN_COMMENTS.items())
    for category, title in [('middleware', '中间件及相机地址'), ('transport', '组播通信'),
                            ('scheduler', '检测调度'), ('voice', '语音播报'),
                            ('commands', '命令模板'), ('led', '显示屏'), ('capture', '拉图轮询')]:
        lines.extend(['', '## ' + title])
        for key, note in PARAMETER_DESCRIPTIONS.items():
            if key.split('.')[0] != category:
                continue
            lines.extend(['', '### ' + key, '', note])
            fields = FIELD_DESCRIPTIONS.get(key, {})
            if fields:
                lines.extend(['', '| JSON字段路径 | 中文说明 |', '|---|---|'])
                lines.extend(f'| `{field}` | {text} |' for field, text in fields.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'已生成中文参数说明：{path}')


if __name__ == '__main__':
    export(ROOT / 'docs' / 'config参数说明.md')
