"""验证修复后的月龄岁混用"""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


async def _no_llm(p, what='', retries=3, stage=None):
    raise AssertionError(f'不该调用 LLM（{what}）')


async def main():
    import app.publish.llm as L
    L.ask_json = _no_llm

    from app.publish.extract import _merge_vision

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

    result = info['sizeMeasurements']
    expect = {
        '6m': {'衣长': 40},
        '12m': {'衣长': 44},
        '2y': {'衣长': 48},
        '3y': {'衣长': 52}
    }

    print(f"实际: {result}")
    print(f"期望: {expect}")
    print(f"匹配: {result == expect}")

    if result != expect:
        print("\n差异:")
        for k in set(result.keys()) | set(expect.keys()):
            if result.get(k) != expect.get(k):
                print(f"  {k}: {result.get(k)} vs {expect.get(k)}")


if __name__ == '__main__':
    asyncio.run(main())
