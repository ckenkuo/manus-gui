# -*- coding: utf-8 -*-
"""类目/属性缓存层的离线单测（纯磁盘读写，不碰浏览器、不碰 LLM）。

这一层的正确性直接决定属性能不能填对：缓存的 options 会被 _validate_attr_changes
当成「页面上真实存在的选项」来放行，也会被 _rebuild_main_comp 拿去做纤维匹配。
所以下面几条不变式必须钉住——尤其是 slug 的路径哈希（不同分支同名叶子类目不能
串台）和「不存 current / 不存空 options」。
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


# ---- cat_slug ---------------------------------------------------------------

def test_slug_清洗Windows非法字符():
    slug = cache.cat_slug('上装/材质:其他?"<>|', PATH_A)
    assert not any(ch in slug for ch in '\\/:*?"<>|')


def test_slug_剥掉末尾的点与空格():
    """Windows 不允许文件名以点或空格结尾，open() 会静默落到另一个名字上。"""
    slug = cache.cat_slug("上装（其他）. ", PATH_A)
    name = slug.rsplit("-", 1)[0]      # 去掉哈希后缀再看
    assert not name.endswith((".", " "))


def test_slug_同名叶子不同路径不串台():
    """「其他（...）」这类叶子名跨分支重复出现，只按叶子名分文件会把两套 options
    混进一个文件，_validate_attr_changes 的 options 闸门会因此放过不存在的选项。"""
    a = cache.cat_slug("其他（上装）", ["女装", "女士上装", "其他（上装）"])
    b = cache.cat_slug("其他（上装）", ["男装", "男士上装", "其他（上装）"])
    assert a != b


def test_slug_站点不同不串台():
    assert cache.cat_slug("女童针织套头衫", PATH_A, "全球") != \
           cache.cat_slug("女童针织套头衫", PATH_A, "美国")


def test_slug_空叶子返回空():
    assert cache.cat_slug("", PATH_A) == ""
    assert cache.cat_slug(None, PATH_A) == ""


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


# ---- 属性选项 ---------------------------------------------------------------

def _attrs(*items):
    """构造 dump_attrs 形状的属性行（带 current/visible，用来验证它们不落盘）。"""
    out = []
    for label, opts in items:
        out.append({"label": label, "options": list(opts), "required": True,
                    "current": "前一批填的值", "numValues": ["90"],
                    "visible": True})
    return out


def test_属性选项读写往返():
    cache.save_attr_options("女童针织套头衫", PATH_A,
                            _attrs(("织造方式", ["梭织", "针织"])))
    got = cache.load_attr_options("女童针织套头衫", PATH_A)
    assert got == {"织造方式": ["梭织", "针织"]}


def test_落盘不含current和numValues和visible():
    """current 带着前一批已填的值，存进去会让下个商品的 LLM 以为表单已填好。"""
    cache.save_attr_options("女童针织套头衫", PATH_A,
                            _attrs(("织造方式", ["梭织", "针织"])))
    slug = cache.cat_slug("女童针织套头衫", PATH_A)
    with open(cache._attrs_path(slug), encoding="utf-8") as f:
        raw = json.load(f)
    row = raw["rows"][0]
    assert set(row) == {"label", "required", "options"}
    assert "前一批填的值" not in json.dumps(raw, ensure_ascii=False)


def test_属性选项按label并集合并():
    """单次 dump_attrs 默认只读必填项，而平台会调整同一类目的必填集合、探查时还会
    用 required_only=False 读到非必填行，各次读到的行集不同；覆盖会把上次辛苦读到的
    行冲掉。"""
    cache.save_attr_options("女童针织套头衫", PATH_A, _attrs(
        ("织造方式", ["梭织", "针织"]), ("季节", ["春/秋"]), ("领型", ["圆领"])))
    cache.save_attr_options("女童针织套头衫", PATH_A, _attrs(
        ("季节", ["春/秋", "夏"]), ("图案", ["条纹", "纯色"])))
    got = cache.load_attr_options("女童针织套头衫", PATH_A)
    assert set(got) == {"织造方式", "季节", "领型", "图案"}
    assert got["织造方式"] == ["梭织", "针织"]      # 老行没被冲掉
    assert got["季节"] == ["春/秋", "夏"]            # 同名行取新的


@pytest.mark.parametrize("reason", ["row-hidden", "optional-skipped", "open-failed"])
def test_空options的行不落盘(reason):
    """只存非空清单，「文件里没这个 label」才能永远等价于「这行要现场读」。"""
    cache.save_attr_options("女童针织套头衫", PATH_A, [
        {"label": "品牌名", "options": [], "optionsEmptyReason": reason},
        {"label": "织造方式", "options": ["梭织"], "required": True},
    ])
    got = cache.load_attr_options("女童针织套头衫", PATH_A)
    assert "品牌名" not in got
    assert got["织造方式"] == ["梭织"]


def test_全是空options时不建文件():
    cache.save_attr_options("女童针织套头衫", PATH_A,
                            [{"label": "品牌名", "options": []}])
    assert cache.load_attr_options("女童针织套头衫", PATH_A) == {}


def test_update_attr_row是覆盖不是并集():
    """重读的结果就是当前真相，旧的过期选项必须消失。"""
    cache.save_attr_options("女童针织套头衫", PATH_A, _attrs(
        ("织造方式", ["梭织", "针织"]), ("季节", ["春/秋"])))
    cache.update_attr_row("女童针织套头衫", PATH_A, "织造方式", ["针织", "钩织"])
    got = cache.load_attr_options("女童针织套头衫", PATH_A)
    assert got["织造方式"] == ["针织", "钩织"]      # 梭织没了
    assert got["季节"] == ["春/秋"]                  # 其余行不动


def test_catPath不符当未命中():
    """文件被手工改过、或 schema 升级后名字口径变了时，退化成未命中而不是喂错数据。"""
    cache.save_attr_options("女童针织套头衫", PATH_A,
                            _attrs(("织造方式", ["梭织"])))
    slug = cache.cat_slug("女童针织套头衫", PATH_A)
    data = json.load(open(cache._attrs_path(slug), encoding="utf-8"))
    data["catPath"] = PATH_B                      # 人为篡改成另一条路径
    with open(cache._attrs_path(slug), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    assert cache.load_attr_options("女童针织套头衫", PATH_A) == {}


def test_属性文件损坏当未命中不抛():
    slug = cache.cat_slug("女童针织套头衫", PATH_A)
    os.makedirs(os.path.dirname(cache._attrs_path(slug)), exist_ok=True)
    with open(cache._attrs_path(slug), "w", encoding="utf-8") as f:
        f.write("{坏的")
    assert cache.load_attr_options("女童针织套头衫", PATH_A) == {}


def test_空叶子不读不写():
    cache.save_attr_options("", PATH_A, _attrs(("织造方式", ["梭织"])))
    assert cache.load_attr_options("", PATH_A) == {}


def test_原子写不留tmp残留():
    cache.remember_category(PATH_A, "标题")
    cache.save_attr_options("女童针织套头衫", PATH_A,
                            _attrs(("织造方式", ["梭织"])))
    leftovers = []
    for root, _dirs, files in os.walk(cache.CACHE_DIR):
        leftovers += [f for f in files if f.endswith(".tmp")]
    assert leftovers == []


# ---- 统计与清理 -------------------------------------------------------------

def test_cache_stats统计():
    cache.remember_category(PATH_A, "标题")
    cache.save_attr_options("女童针织套头衫", PATH_A, _attrs(
        ("织造方式", ["梭织"]), ("季节", ["春/秋"])))
    st = cache.cache_stats()
    assert st["paths"] == 1
    assert st["attrCategories"] == 1
    assert st["attrRows"] == 2


def test_clear单个属性文件():
    cache.remember_category(PATH_A, "标题")
    cache.save_attr_options("女童针织套头衫", PATH_A, _attrs(("织造方式", ["梭织"])))
    cache.save_attr_options("女童长裤套装", PATH_B, _attrs(("套装组成件数", ["2件"])))
    slug = cache.cat_slug("女童针织套头衫", PATH_A)
    cache.clear(slug)
    assert cache.load_attr_options("女童针织套头衫", PATH_A) == {}
    assert cache.load_attr_options("女童长裤套装", PATH_B) != {}   # 另一个还在
    assert len(cache.load_categories()) == 1                       # 路径清单不动


def test_clear全清():
    cache.remember_category(PATH_A, "标题")
    cache.save_attr_options("女童针织套头衫", PATH_A, _attrs(("织造方式", ["梭织"])))
    cache.clear()
    assert cache.load_categories() == []
    assert cache.load_attr_options("女童针织套头衫", PATH_A) == {}


def test_clear空目录不抛():
    assert cache.clear() == {"categories": False, "attrFiles": []}


def test_未读全的选项拒收():
    """虚拟列表没滚到底时读到的是截断清单（如 67 项成分只读到首屏 10 条）。
    这种清单进了缓存，之后每个同类目商品都会拿缺项的 options 做校验与纤维匹配，
    而且没有任何环节会发现——比不缓存糟得多。"""
    cache.save_attr_options("女童针织套头衫", PATH_A, [
        {"label": "上装成分", "required": True, "options": ["棉", "腈纶"],
         "optionsComplete": False},
        {"label": "织造方式", "required": True, "options": ["梭织", "针织"],
         "optionsComplete": True},
    ])
    got = cache.load_attr_options("女童针织套头衫", PATH_A)
    assert "上装成分" not in got
    assert got["织造方式"] == ["梭织", "针织"]


def test_缺optionsComplete字段时视为完整():
    """向后兼容：老调用方不传这个字段，不该因此一行都存不下。"""
    cache.save_attr_options("女童针织套头衫", PATH_A, [
        {"label": "织造方式", "required": True, "options": ["梭织"]}])
    assert cache.load_attr_options("女童针织套头衫", PATH_A) == {"织造方式": ["梭织"]}
