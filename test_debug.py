"""调试月龄岁混用用例"""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


async def _no_llm(p, what='', retries=3, stage=None):
    raise AssertionError(f'不该调用 LLM（{what}）')


async def main():
    import app.publish.llm as L
    L.ask_json = _no_llm

    from app.publish.extract import _merge_vision, _rows_to_measurements

    # 先直接测 _rows_to_measurements
    print("=== 直接测 _rows_to_measurements ===")
    rows_obj = {
        'params': ['衣长'],
        'rows': [[40], [44], [48], [52]]
    }
    sizes = ['2y', '6m', '3y', '12m']

    out, note = await _rows_to_measurements(rows_obj, sizes, title="测试")
    print(f"输出: {out}")
    print(f"note: {note}")
    print()

    # 再测完整 _merge_vision
    print("=== 测 _merge_vision ===")
    info = {
        'imageUnderstanding': {},
        'sizeChart': {},
        'sizeMeasurements': {},
        'complianceNotes': {},
        'sizes': ['2y', '6m', '3y', '12m']
    }
    vision = {
        'sizeMeasurementsRows': {
            'params': ['衣长'],
            'rows': [[40], [44], [48], [52]]
        }
    }

    await _merge_vision(info, vision, {})
    print(f"info['sizeMeasurements'] = {info['sizeMeasurements']}")
    print(f"期望: {{'6m': {{'衣长': 40}}, '12m': {{'衣长': 44}}, '2y': {{'衣长': 48}}, '3y': {{'衣长': 52}}}}")


if __name__ == '__main__':
    asyncio.run(main())
