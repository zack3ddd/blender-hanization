bl_info = {
    "name": "繁化助手（簡中→繁體）",
    "author": "Zack3D",
    "version": (0, 3, 1),
    "blender": (4, 2, 0),
    "location": "Edit > Preferences > Add-ons > 繁化助手",
    "description": "把 Blender 內建的簡體中文介面換成繁體，並可選擇性套用習慣用語替換。以檔案替換方式運作，可一鍵還原。",
    "category": "User Interface",
}

import bpy
import os
import shutil
import tempfile

# 你原本的 24 條習慣用語替換（預設詞彙表）
DEFAULT_GLOSSARY = [
    ("菜單", "選單"), ("窗口", "視窗"), ("鼠標", "滑鼠"), ("分辨率", "解析度"),
    ("激活", "啟動"), ("批量", "批次"), ("屏幕", "螢幕"), ("攝像機", "攝影機"),
    ("撤銷", "復原"), ("內存", "記憶體"), ("緩存", "快取"), ("音頻", "音訊"),
    ("視頻", "影片"), ("加載", "載入"), ("保存", "儲存"), ("默認", "預設"),
    ("用戶", "使用者"), ("全屏", "全螢幕"), ("插件", "附加元件"), ("摳像", "去背"),
    ("導入", "匯入"), ("導出", "匯出"), ("文本", "文字"), ("添加", "新增"),
]



# ─────────────────────────────────────────────────────────────
# 路徑 / 檔案處理
# ─────────────────────────────────────────────────────────────
def _base_mo_path():
    return os.path.join(os.path.dirname(__file__), "data", "base.mo")


def _looks_simplified(path):
    """粗略判斷 .mo 是否為原始簡體（簡體字多於繁體字）。用來防止把繁體誤備份成原檔。"""
    try:
        from . import polib
        mo = polib.mofile(path)
        blob = "".join((e.msgstr or "") for e in mo)
        s = sum(blob.count(c) for c in "编选视设转")
        t = sum(blob.count(c) for c in "編選視設轉")
        return s >= t
    except Exception:
        return False


def _target_mo_path():
    """找到 Blender 內建簡體中文的 blender.mo 位置。"""
    locale_dir = os.path.join(bpy.utils.system_resource("DATAFILES"), "locale")
    if not os.path.isdir(locale_dir):
        return None, locale_dir
    for name in os.listdir(locale_dir):
        if name.lower() in ("zh_hans", "zh_cn"):
            p = os.path.join(locale_dir, name, "LC_MESSAGES", "blender.mo")
            if os.path.exists(p):
                return p, locale_dir
    return None, locale_dir


def _apply_glossary(text, pairs):
    for src, dst in pairs:
        if src:
            text = text.replace(src, dst)
    return text


def _get_prefs():
    return bpy.context.preferences.addons[__package__].preferences


# ─────────────────────────────────────────────────────────────
# 核心：寫入 / 還原
# ─────────────────────────────────────────────────────────────
def _backup_path():
    """原始簡體語系檔的備份位置（存在使用者設定區，非管理員可寫、重裝外掛也不會消失）。"""
    d = bpy.utils.user_resource("CONFIG", path="zack_hanization", create=True)
    return os.path.join(d, "original_zh_HANS.mo")


def _elevated_copy(src, dst):
    """
    以系統管理員權限把 src 複製到 dst（會跳出一次 Windows UAC 授權視窗）。
    回傳 (ok, message)。使用者按「否」會回傳取消訊息。
    """
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    # 用隱藏的 cmd 執行一次複製；/Y 覆蓋不詢問
    params = '/c copy /Y "%s" "%s"' % (src, dst)
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "cmd.exe"
    sei.lpParameters = params
    sei.nShow = 0  # SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.GetLastError()
        if err == 1223:  # ERROR_CANCELLED
            return False, "你在授權視窗按了『否』，尚未寫入。要套用請再按一次並選『是』。"
        return False, "無法叫出授權視窗（錯誤碼 %d）。" % err

    if sei.hProcess:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 30000)
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        if code.value != 0:
            return False, "複製失敗（回傳碼 %d）。" % code.value
    return True, ""


def _write_target(new_mo):
    """把 new_mo 寫到 Blender 的語系檔位置。先試直接寫，不行就跳 UAC 授權。"""
    target, locale_dir = _target_mo_path()
    if not target:
        return False, "找不到 Blender 內建簡體語系檔（locale 目錄：%s）" % locale_dir

    # 第一次動手前，先把原始簡體檔備份起來（讀 Program Files 一般允許）
    # 防呆：只有目前檔「確實是簡體」時才備份，避免把已轉換的繁體誤存成原檔而污染還原
    backup = _backup_path()
    if not os.path.exists(backup) and _looks_simplified(target):
        try:
            shutil.copy2(target, backup)
        except Exception as ex:
            return False, "備份原始語系檔失敗：%s" % ex

    # 先試直接寫（Blender 裝在可寫位置就完全不用授權）
    try:
        shutil.copy2(new_mo, target)
        return True, "已寫入（免授權）。"
    except PermissionError:
        pass
    except Exception as ex:
        return False, "寫入失敗：%s" % ex

    # 沒權限 → 跳一次 Windows 授權視窗
    ok, emsg = _elevated_copy(new_mo, target)
    if ok:
        return True, "已透過授權寫入。"
    return False, emsg


def _do_apply(prefs):
    """回傳 (ok, message)。"""
    base_mo = _base_mo_path()
    if not os.path.exists(base_mo):
        return False, "外掛內建 base.mo 遺失，請重新安裝外掛。"

    # 產生要寫入的 .mo（純底，或底＋詞彙）到暫存區
    tmp = os.path.join(tempfile.gettempdir(), "zack_hanize_out.mo")
    try:
        if prefs.enable_glossary:
            from . import polib
            mo = polib.mofile(base_mo)
            pairs = [(p.src, p.dst) for p in prefs.glossary if p.src]
            for e in mo:
                if e.msgstr:
                    e.msgstr = _apply_glossary(e.msgstr, pairs)
                if e.msgstr_plural:
                    for k in e.msgstr_plural:
                        e.msgstr_plural[k] = _apply_glossary(e.msgstr_plural[k], pairs)
            mo.save(tmp)
        else:
            shutil.copy2(base_mo, tmp)
    except Exception as ex:
        return False, "產生語系檔失敗：%s" % ex

    ok, msg = _write_target(tmp)
    if not ok:
        return False, msg
    return True, "已套用繁化。"


def _do_restore():
    backup = _backup_path()
    if not os.path.exists(backup):
        return False, "找不到備份（可能尚未套用過繁化）。"
    ok, msg = _write_target(backup)
    if not ok:
        return False, msg
    return True, "已還原為原本簡體。"


def _reload_to(context, target_lang):
    """把語言切走再切回，逼 Blender 重新從磁碟讀取剛寫好的 .mo（嘗試免重開）。"""
    try:
        view = context.preferences.view
        view.use_translate_interface = True
        via = "en_US" if target_lang != "en_US" else "zh_HANS"
        view.language = via
        view.language = target_lang
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Operators
# ─────────────────────────────────────────────────────────────
class HANIZE_OT_apply(bpy.types.Operator):
    bl_idname = "hanize.apply"
    bl_label = "套用繁化到 Blender"
    bl_description = "把介面換成繁體（若勾選則一併套用習慣用語）。需重啟 Blender 生效。"

    def execute(self, context):
        ok, msg = _do_apply(_get_prefs())
        if ok:
            _reload_to(context, "zh_HANS")  # 自動切到簡中槽並重載，嘗試免重開
            self.report({'INFO'}, "已套用繁化。")
        else:
            self.report({'ERROR'}, msg)
        return {'FINISHED'}


class HANIZE_OT_restore(bpy.types.Operator):
    bl_idname = "hanize.restore"
    bl_label = "還原原本簡體"
    bl_description = "把語系檔還原成 Blender 原本的簡體中文。需重啟 Blender 生效。"

    def execute(self, context):
        ok, msg = _do_restore()
        if ok:
            _reload_to(context, context.preferences.view.language)  # 重載目前語言
            self.report({'INFO'}, "已還原為原本簡體。")
        else:
            self.report({'ERROR'}, msg)
        return {'FINISHED'}


class HANIZE_OT_glossary_add(bpy.types.Operator):
    bl_idname = "hanize.glossary_add"
    bl_label = "新增詞彙"

    def execute(self, context):
        _get_prefs().glossary.add()
        return {'FINISHED'}


class HANIZE_OT_glossary_remove(bpy.types.Operator):
    bl_idname = "hanize.glossary_remove"
    bl_label = "移除此列"
    index: bpy.props.IntProperty()

    def execute(self, context):
        _get_prefs().glossary.remove(self.index)
        return {'FINISHED'}


class HANIZE_OT_glossary_reset(bpy.types.Operator):
    bl_idname = "hanize.glossary_reset"
    bl_label = "重設為預設 24 條"

    def execute(self, context):
        prefs = _get_prefs()
        prefs.glossary.clear()
        for s, d in DEFAULT_GLOSSARY:
            it = prefs.glossary.add()
            it.src, it.dst = s, d
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────
# Preferences UI
# ─────────────────────────────────────────────────────────────
class HANIZE_glossary_item(bpy.types.PropertyGroup):
    src: bpy.props.StringProperty(name="簡")
    dst: bpy.props.StringProperty(name="繁")


class HANIZE_prefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    enable_glossary: bpy.props.BoolProperty(
        name="套用時包含習慣用語替換（預設關閉）",
        default=False,
    )
    glossary: bpy.props.CollectionProperty(type=HANIZE_glossary_item)

    def draw(self, context):
        layout = self.layout

        # 動作區
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("hanize.apply", icon='CHECKMARK')
        row.operator("hanize.restore", icon='LOOP_BACK')

        layout.prop(self, "enable_glossary")

        # 詞彙表（兩行式）
        box = layout.box()
        box.enabled = self.enable_glossary
        head = box.row(align=True)
        head.label(text="習慣用語替換表（簡 → 繁）")
        head.operator("hanize.glossary_reset", icon='FILE_REFRESH', text="重設")
        head.operator("hanize.glossary_add", icon='ADD', text="新增")

        tbl = box.column(align=True)
        # 表頭
        hr = tbl.row(align=True)
        h1 = hr.split(factor=0.07, align=True)
        h1.label(text="#")
        h2 = h1.split(factor=0.5, align=True)
        h2.label(text="簡體（原詞）")
        h3 = h2.split(factor=0.92, align=True)
        h3.label(text="繁體（改成）")
        h3.label(text="")
        # 各列：編號 | 簡 | 繁 | 刪除，欄位左右對齊
        for i, item in enumerate(self.glossary):
            r = tbl.row(align=True)
            c1 = r.split(factor=0.07, align=True)
            c1.label(text="%d" % (i + 1))
            c2 = c1.split(factor=0.5, align=True)
            c2.prop(item, "src", text="")
            c3 = c2.split(factor=0.92, align=True)
            c3.prop(item, "dst", text="")
            op = c3.operator("hanize.glossary_remove", text="", icon='X')
            op.index = i

        layout.separator()
        col = layout.column(align=True)
        col.label(text="按「套用繁化到 Blender」套用。改詞彙表或切換勾選後，再按一次「套用」。", icon='INFO')
        col.label(text="寫入若跳出 Windows 授權視窗，按「是」即可。", icon='INFO')

        layout.separator()
        foot = layout.column(align=True)
        foot.scale_y = 0.8
        foot.label(text="此翻譯工具僅供個人學習使用，感謝台灣官方繁體譯者。")
        foot.label(text="簡體基礎來自並感謝 BlenderCN 團隊（版權屬 www.blendercn.org，GNU GPL）。")


classes = (
    HANIZE_glossary_item,
    HANIZE_OT_apply,
    HANIZE_OT_restore,
    HANIZE_OT_glossary_add,
    HANIZE_OT_glossary_remove,
    HANIZE_OT_glossary_reset,
    HANIZE_prefs,
)


def _populate_defaults():
    # 延遲載入，避開 register() 當下取不到 preferences 的時序問題
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return 0.1
    if len(prefs.glossary) == 0:
        for s, d in DEFAULT_GLOSSARY:
            it = prefs.glossary.add()
            it.src, it.dst = s, d
    return None


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.app.timers.register(_populate_defaults, first_interval=0.1)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
