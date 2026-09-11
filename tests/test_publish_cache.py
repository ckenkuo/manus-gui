# -*- coding: utf-8 -*-
"""类目路径缓存层的离线单测（纯磁盘读写，不碰浏览器、不碰 LLM）。

这一层决定阶段③ 能不能走快路径：缓存里的路径会被 _pick_cached_category 当成候选
交给 LLM，记错了就会把商品选进错的类目。故下面几条不变式必须钉住——尤其是
「同路径重复记只累加 hits、不重复建条目」与「空路径不落盘」。

（属性选项缓存 2026-09-11 已删：选项改由服务端接口现取，见 attributes/server_options；
它原先那套「截断清单不许进缓存」的用例随之移除。）
"""
import json
import os

import pytest

from app.publish import cache


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """缓存目录重定向到临时目录，不污染真实 workspace/publish-cache/。"""
    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path / "publish-cache"))


PATH_A = ["服装、鞋靴和珠宝饰品", "女童时尚", "女童服装",
          "女童毛衣、针织衫", "女童针织套头衫"]
PATH_B = ["服装、鞋靴和珠宝饰品", "女童时尚", "女童服装",
          "女童时尚套装", "女童长裤套装"]


# ---- 类目路径清单 ------------------------------------------------------------

def test_类目路径读写往返():
    cache.remember_category(PATH_A, "针织毛衣女童套头")
    got = cache.load_categories()
    assert len(got) == 1
    assert got[0]["path"] == PATH_A
    assert got[0]["leaf"] == "女童针织套头衫"
    assert got[0]["hits"] == 1


def test_同路径记两次只有一条且hits累加():
    cache.remember_category(PATH_A, "标题一")
    cache.remember_category(PATH_A, "标题二")
    got = cache.load_categories()
    assert len(got) == 1 and got[0]["hits"] == 2
    assert got[0]["titles"] == ["标题二", "标题一"]      # 最近的在前


def test_标题样本去重且有上限():
    for i in range(6):
        cache.remember_category(PATH_A, f"标题{i}")
    cache.remember_category(PATH_A, "标题0")
    titles = cache.load_categories()[0]["titles"]
    assert len(titles) <= 3
    assert len(set(titles)) == len(titles)


def test_空路径不落盘():
    cache.remember_category([])
    cache.remember_category(["", "  "])
    assert cache.load_categories() == []


def test_类目文件损坏当空跑不抛():
    os.makedirs(cache.CACHE_DIR, exist_ok=True)
    with open(os.path.join(cache.CACHE_DIR, "categories.json"),
              "w", encoding="utf-8") as f:
        f.write("{截断的 json")
    assert cache.load_categories() == []


def test_目录不存在时读为空():
    assert cache.load_categories() == []


def test_prompt_paths按最近使用截断():
    for i in range(5):
        cache.remember_category(PATH_A[:-1] + [f"叶子{i}"], f"标题{i}")
    got = cache.prompt_paths(limit=2)
    assert len(got) == 2
    # 最后写入的最近被用到，必须留下
    assert got[0]["leaf"] == "叶子4"




# ---- 类目 id 随路径一起记 ----------------------------------------------------
#
# 阶段④ 查属性选项要带上页面上当前生效的叶子类目 id，而命中缓存路径时阶段③ 并没有
# 调接口拿候选，只能从缓存条目里取（见 cache.cat_ids_for）。这两条钉住「记得住」
# 与「级数对不上就不记」——错位的一串 id 比没有更糟：阶段④ 会拿它去查一个别的类目。

def test_记下的catIds能按路径取回():
    cache.remember_category(PATH_A, "标题", ["1", "2", "3", "4", "11717"])
    assert cache.cat_ids_for(PATH_A) == ["1", "2", "3", "4", "11717"]


def test_catIds级数对不上就不记():
    """少一级（接口只给到中间某级）就不落盘：错位的 id 串会让阶段④ 查错类目，
    而查出来的属性清单「看起来正常」，没有任何环节能发现。"""
    cache.remember_category(PATH_A, "标题", ["1", "2", "3"])
    assert cache.cat_ids_for(PATH_A) == []


def test_老条目没有catIds取回空():
    """向后兼容：此前记下的路径没这个键 → 空 → 阶段④ 退回按草稿已保存的类目查。"""
    cache.remember_category(PATH_A, "标题")
    assert cache.cat_ids_for(PATH_A) == []


# ---- 统计与清理 -------------------------------------------------------------

def test_cache_stats统计():
    cache.remember_category(PATH_A, "标题")
    st = cache.cache_stats()
    assert st["paths"] == 1
    assert [c["path"] for c in st["categories"]] == [PATH_A]


def test_cache_stats空缓存():
    assert cache.cache_stats() == {"paths": 0, "categories": []}


def test_clear全清():
    cache.remember_category(PATH_A, "标题")
    cache.remember_category(PATH_B, "标题")
    assert cache.clear() == {"categories": True}
    assert cache.load_categories() == []


def test_clear空目录不抛():
    assert cache.clear() == {"categories": False}
