import sys
sys.path.insert(0, r"D:\mycode\program\deskpet")
from pet import skins
name = skins.prepare_import(r"D:\mycode\program\deskpet\tests\tmp_skin", "测试皮肤")
print("prepared:", name)
paths = skins.build_skin("测试皮肤", 240, 12, log=print)
print("built:", sorted(paths))
print("skins:", sorted(skins.list_skins()))
