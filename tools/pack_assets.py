#!/usr/bin/env python
"""把 PC 侧产物打成端内 assets（M1 的 S1-a）。

产出（默认 `android/play/app/src/main/assets/`）：

    manifest.json               曲库清单：key / 曲名 / 难度 / 音符数 / sil_ref_s / tap_lead_ms / 载荷路径
    payload/<key>.json          瘦身载荷：只保留端内 Scheduler.build() 真正读的键

为什么瘦身：`touches_*.json` 里 `plan` 有 3 万条明细（1.3 MB 里占 1.1 MB），
端内 `Scheduler.parsePayload` **只物化 canvas / screen / events**，plan 纯属白占 assets。
瘦身后 bounded_in = 229 KB（1.32 MB → 229 KB，−83%）。

★ 瘦身必须是**信息无损**的：`events` 每条只留
  `t_ms / action / pointer_id / x / y`（`Scheduler.build()` 第 121-130 行读的就这五个）。
  自测会**全量逐条**比对，不是抽检 —— 少一个字段就是静默改行为。

★ `sil_ref` 是**母带属性**，与设备/轮次无关（`audio_round.reference_silence` 在 wav 上算）。
  已核对：bounded_in 算得 0.865000 s，与真机 timeline 记的 0.865 一致。
  端内零点公式 `zero_dev = onset_ns − sil_ref − L_dev` 直接用这个值 ⇒ **不需要设备参与打包**。

用法（Git Bash，cwd 项目根）：

    .venv/Scripts/python.exe tools/pack_assets.py              # 打包内置的全部曲目
    .venv/Scripts/python.exe tools/pack_assets.py --list       # 只看会打包什么
    .venv/Scripts/python.exe tools/pack_assets.py --out <dir>  # 换输出目录（自测用）
    .venv/Scripts/python.exe tools/pack_assets.py --verify-java # 附带 Java 逐位对拍
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tmp"))

#: 端内 `Scheduler.build()` 读的事件字段（改这里必须同步 Scheduler.java:121-130）。
EVENT_KEYS = ("t_ms", "action", "pointer_id", "x", "y")

#: ★ 自测用的**独立**字段表。**故意不复用 EVENT_KEYS**：
#:   若两边共用一份，从白名单里删掉一个键时「写」和「查」会同时少一项 ⇒ 自测仍绿（空转）。
#:   自测必须站在另一个口径上，才能抓到生产白名单被改窄。
REQUIRED_EVENT_KEYS = ("t_ms", "action", "pointer_id", "x", "y")

#: 端内 `Scheduler.requireGameCanvas` 的同一条判据（相对容差 0.1%）。
#: ⛔ 两条必须同源：一处放宽、另一处收紧都会让"PC 打包通过、端内拒绝开火"（或反过来）。
CANVAS_ASPECT_TOL = 1e-3

#: 起音盲区（秒）：音乐起音 → 端内能派发第一个事件。Ⅱ 类曲的首音落在这里面。
BLIND_S = 0.342

#: 要打包的曲（key = 载荷文件名里 `touches_` 后面那截）。
#:
#: ⛔ 曲名与难度等级**不在这里写死**：PC 侧没有曲库索引（谱面 json 只有
#:   formatVersion/offset/judgeLineList，`music.info.json` 只有采样率与时长），
#:   手写就只能靠编。改成：曲名从 `song` 目录名首段推导，难度从 `chart` 文件名取。
SONGS: tuple[str, ...] = (
    "70mf_ez",
    "70mf_hd",
    "996",
    "996_hd",
    "996_in",
    "abouttheuniverse",
    "abouttheuniverse_hd",
    "abouttheuniverse_in",
    "absolutedisorder",
    "absolutedisorder_at",
    "absolutedisorder_ez",
    "absolutedisorder_in",
    "abyssmotion192333",
    "abyssmotion192333_hd",
    "abyssmotion192333_in",
    "adastraperaspera",
    "adastraperaspera_hd",
    "adastraperaspera_in",
    "afterdawn",
    "afterdawn_hd",
    "afterdawn_in",
    "ajourneytothemoonlight",
    "ajourneytothemoonlight_hd",
    "ajourneytothemoonlight_in",
    "alb",
    "alb_hd",
    "alb_in",
    "aleph0",
    "aleph0_ez",
    "aleph0_hd",
    "aliceinaxxxxxxxx",
    "aliceinaxxxxxxxx_hd",
    "aliceinaxxxxxxxx_in",
    "ametrine",
    "ametrine_at",
    "ametrine_hd",
    "ametrine_in",
    "anotherme",
    "anotherme_hd",
    "anotherme_in",
    "anotherround",
    "anotherround_hd",
    "anotherround_in",
    "antithese",
    "antithese_at",
    "antithese_hd",
    "antithese_in",
    "aphasia",
    "aphasia_hd",
    "aphasia_in",
    "aphroditeschildshortver",
    "aphroditeschildshortver_hd",
    "aphroditeschildshortver_in",
    "apocalypse",
    "apocalypse_hd",
    "apocalypse_in",
    "apocalyptic",
    "apocalyptic_hd",
    "apocalyptic_in",
    "archidoxen",
    "archidoxen_at",
    "archidoxen_hd",
    "archidoxen_in",
    "ark_at",
    "ark_ez",
    "ark_hd",
    "arkhei",
    "arkhei_ez",
    "arkhei_in",
    "artificialexistence",
    "artificialexistence_hd",
    "artificialexistence_in",
    "athaza",
    "athaza_at",
    "athaza_hd",
    "athaza_in",
    "atruthseekercommunicationwithutopiawillbelost",
    "atruthseekercommunicationwithutopiawillbelost_hd",
    "atruthseekercommunicationwithutopiawillbelost_in",
    "avataarreincarnationofkalpa",
    "avataarreincarnationofkalpa_at",
    "avataarreincarnationofkalpa_hd",
    "avataarreincarnationofkalpa_in",
    "aventyr",
    "aventyr_hd",
    "aventyr_in",
    "bangingstrike",
    "bangingstrike_at",
    "bangingstrike_ez",
    "bangingstrike_hd",
    "beethovenblessing",
    "beethovenblessing_hd",
    "beethovenblessing_in",
    "believelightfeat",
    "believelightfeat_hd",
    "believelightfeat_in",
    "bettergraphicanimation",
    "bettergraphicanimation_hd",
    "bettergraphicanimation_in",
    "bitterblossom",
    "bitterblossom_hd",
    "bitterblossom_in",
    "bloom",
    "bloom_hd",
    "bloom_in",
    "bonustime",
    "bonustime_hd",
    "bonustime_in",
    "bougainvillea",
    "bougainvillea_ez",
    "bougainvillea_hd",
    "bounded_in",
    "boundedquietude",
    "boundedquietude_ez",
    "brainhacker",
    "brainhacker_hd",
    "brainhacker_in",
    "bravenotes",
    "bravenotes_hd",
    "bravenotes_in",
    "breakover",
    "breakover_ez",
    "breakover_hd",
    "breakover_in",
    "breakthroughthebarrier",
    "breakthroughthebarrier_hd",
    "breakthroughthebarrier_in",
    "brokensky",
    "brokensky_hd",
    "brokensky_in",
    "burn",
    "burn_ez",
    "burn_hd",
    "bye",
    "bye_hd",
    "bye_in",
    "cereris",
    "cereris_hd",
    "cereris_in",
    "cervelleconnexion",
    "cervelleconnexion_hd",
    "cervelleconnexion_in",
    "chronologika",
    "chronologika_hd",
    "chronologika_in",
    "chronomia",
    "chronomia_hd",
    "chronomia_in",
    "chronoscollapselacampanella",
    "chronoscollapselacampanella_hd",
    "chronoscollapselacampanella_in",
    "chronostasis",
    "chronostasis_hd",
    "chronostasis_in",
    "cipher20",
    "cipher20_hd",
    "cipher20_in",
    "classmemories",
    "classmemories_ez",
    "classmemories_hd",
    "cleyera_ez",
    "cleyera_hd",
    "clockparadox",
    "clockparadox_hd",
    "clockparadox_in",
    "colorfuldays",
    "colorfuldays_hd",
    "colorfuldays_in",
    "cometfeat",
    "cometfeat_hd",
    "cometfeat_in",
    "computeitwithsomedevilishalcoholicsteampunkengines",
    "computeitwithsomedevilishalcoholicsteampunkengines_hd",
    "computeitwithsomedevilishalcoholicsteampunkengines_in",
    "concvssion",
    "concvssion_hd",
    "concvssion_in",
    "confusion",
    "confusion_hd",
    "confusion_in",
    "cosmicdustygirl",
    "cosmicdustygirl_hd",
    "cosmicdustygirl_in",
    "cravewave",
    "cravewave_hd",
    "cravewave_in",
    "credits_at",
    "credits_ez",
    "credits_hd",
    "credits_in",
    "cristalisia",
    "cristalisia_ez",
    "cristalisia_hd",
    "cross_ez",
    "cross_hd",
    "crushbeta",
    "crushbeta_hd",
    "crushbeta_in",
    "cryogenic",
    "cryogenic_hd",
    "cryogenic_in",
    "cryout",
    "cryout_hd",
    "cryout_in",
    "csqn",
    "csqn_ez",
    "csqn_hd",
    "cthugha",
    "cthugha_at",
    "cthugha_hd",
    "cthugha_in",
    "cuvism",
    "cuvism_at",
    "cuvism_hd",
    "cuvism_in",
    "dancewithsilence",
    "dancewithsilence_hd",
    "dancewithsilence_in",
    "dash",
    "dash_hd",
    "dash_in",
    "dataerr0r",
    "dataerr0r_hd",
    "dataerr0r_in",
    "dbdoll",
    "dbdoll_ez",
    "dbdoll_hd",
    "dbdoll_in",
    "deadsoul",
    "deadsoul_hd",
    "deadsoul_in",
    "demiurge",
    "demiurge_ez",
    "demiurge_hd",
    "demonkin",
    "demonkin_hd",
    "demonkin_in",
    "derrichter",
    "derrichter_at",
    "derrichter_hd",
    "derrichter_in",
    "destruction321",
    "destruction321_at",
    "destruction321_ez",
    "destruction321_hd",
    "desultorysignals",
    "desultorysignals_at",
    "desultorysignals_hd",
    "desultorysignals_in",
    "devastatinghistory",
    "devastatinghistory_hd",
    "devastatinghistory_in",
    "deviationshortver",
    "deviationshortver_hd",
    "deviationshortver_in",
    "devotion",
    "devotion_hd",
    "devotion_in",
    "diamonddust",
    "diamonddust_at",
    "diamonddust_hd",
    "diamonddust_in",
    "diamondeyes",
    "diamondeyes_hd",
    "diamondeyes_in",
    "disorder",
    "disorder_hd",
    "disorder_in",
    "distort_at",
    "distort_ez",
    "distort_hd",
    "dlyrotz",
    "dlyrotz_hd",
    "dlyrotz_in",
    "dontneveraround",
    "dontneveraround_hd",
    "dontneveraround_in",
    "doppelganger",
    "doppelganger_hd",
    "doppelganger_in",
    "doubleagent",
    "doubleagent_hd",
    "doubleagent_in",
    "dreamland",
    "dreamland_hd",
    "dreamland_in",
    "dropit",
    "dropit_ez",
    "dropit_hd",
    "elcondorpasaphigrosedit",
    "elcondorpasaphigrosedit_hd",
    "elcondorpasaphigrosedit_in",
    "electron",
    "electron_hd",
    "electron_in",
    "elltimobaile",
    "elltimobaile_hd",
    "elltimobaile_in",
    "eltaw",
    "eltaw_hd",
    "eltaw_in",
    "endme",
    "endme_hd",
    "endme_in",
    "energysynergymatrix",
    "energysynergymatrix_hd",
    "energysynergymatrix_in",
    "energytrixxx",
    "energytrixxx_hd",
    "energytrixxx_in",
    "enginexstartmelodymix",
    "enginexstartmelodymix_hd",
    "enginexstartmelodymix_in",
    "entrancetothechaos",
    "entrancetothechaos_ez",
    "entrancetothechaos_hd",
    "entrancetothechaos_in",
    "erabyeconnec10n",
    "erabyeconnec10n_at",
    "erabyeconnec10n_hd",
    "erabyeconnec10n_in",
    "eradicationcatastrophe",
    "eradicationcatastrophe_hd",
    "eradicationcatastrophe_in",
    "eternalsnow",
    "eternalsnow_hd",
    "eternalsnow_in",
    "evanescent",
    "evanescent_hd",
    "evanescent_in",
    "exoplanetarymirage",
    "exoplanetarymirage_at",
    "exoplanetarymirage_hd",
    "exoplanetarymirage_in",
    "feast",
    "feast_hd",
    "feast_in",
    "finalstep",
    "finalstep_hd",
    "finalstep_in",
    "findme",
    "findme_ez",
    "findme_hd",
    "fixationstowardthestars",
    "fixationstowardthestars_hd",
    "fixationstowardthestars_in",
    "flutterecho",
    "flutterecho_hd",
    "flutterecho_in",
    "fracturedangel",
    "fracturedangel_hd",
    "fracturedangel_in",
    "freakyundulationsnobleknightsoftune",
    "freakyundulationsnobleknightsoftune_hd",
    "freakyundulationsnobleknightsoftune_in",
    "frozenheart",
    "frozenheart_hd",
    "frozenheart_in",
    "fuliautobuster",
    "fuliautobuster_hd",
    "fuliautobuster_in",
    "fuliautoshooter",
    "fuliautoshooter_hd",
    "fuliautoshooter_in",
    "futuremind",
    "futuremind_hd",
    "futuremind_in",
    "getback",
    "getback_hd",
    "getback_in",
    "getready",
    "getready_hd",
    "getready_in",
    "glaciaxion",
    "glaciaxion_hd",
    "glaciaxion_in",
    "goldtown",
    "goldtown_hd",
    "goldtown_in",
    "goodbounce",
    "goodbounce_hd",
    "goodbounce_in",
    "goodfortune",
    "goodfortune_ez",
    "goodfortune_in",
    "goodrage",
    "goodrage_ez",
    "goodrage_hd",
    "goodtek",
    "goodtek_ez",
    "goodtek_hd",
    "goodworld",
    "goodworld_hd",
    "goodworld_in",
    "grimheart",
    "grimheart_hd",
    "grimheart_in",
    "gungnir_ez",
    "gungnir_hd",
    "gvnglittervomitusandneon",
    "gvnglittervomitusandneon_hd",
    "gvnglittervomitusandneon_in",
    "hardcorekwaya",
    "hardcorekwaya_hd",
    "hazard",
    "hazard_hd",
    "hazard_in",
    "hckwaya_ez",
    "helios",
    "helios_hd",
    "helios_in",
    "horizonblue",
    "horizonblue_hd",
    "horizonblue_in",
    "human",
    "human_hd",
    "human_in",
    "hydra",
    "hydra_at",
    "hydra_hd",
    "hydra_in",
    "igallta",
    "igallta_at",
    "igallta_hd",
    "igallta_in",
    "ilartifact",
    "ilartifact_hd",
    "ilartifact_in",
    "implexrough",
    "implexrough_hd",
    "implexrough_in",
    "imustsayno",
    "imustsayno_ez",
    "imustsayno_in",
    "incyde",
    "incyde_hd",
    "incyde_in",
    "indeliblescar",
    "indeliblescar_at",
    "indeliblescar_ez",
    "indeliblescar_hd",
    "inferior",
    "inferior_ez",
    "inferior_in",
    "infiniteenerzyoverdoze",
    "infiniteenerzyoverdoze_ez",
    "infiniteenerzyoverdoze_hd",
    "infiniteenerzyoverdoze_in",
    "infinityheaven",
    "infinityheaven_hd",
    "infinityheaven_in",
    "init_ez",
    "init_hd",
    "introduction",
    "invertedworld",
    "invertedworld_hd",
    "invertedworld_in",
    "jinglebell",
    "jinglebell_ez",
    "jinglebell_in",
    "journeywithyou",
    "journeywithyou_hd",
    "journeywithyou_in",
    "junxionbetweenlifeanddeathvipmix",
    "junxionbetweenlifeanddeathvipmix_hd",
    "junxionbetweenlifeanddeathvipmix_in",
    "kerberos",
    "kerberos_hd",
    "kerberos_in",
    "khalid",
    "khalid_hd",
    "khalid_in",
    "khronostasiskatharsis",
    "khronostasiskatharsis_hd",
    "khronostasiskatharsis_in",
    "kirakiranoelstory",
    "kirakiranoelstory_ez",
    "kirakiranoelstory_hd",
    "kizunaresolution",
    "kizunaresolution_hd",
    "kizunaresolution_in",
    "kmoevip",
    "kmoevip_ez",
    "kmoevip_hd",
    "komplexe",
    "komplexe_hd",
    "komplexe_in",
    "labyrinthinkowloonwalledworld",
    "labyrinthinkowloonwalledworld_hd",
    "labyrinthinkowloonwalledworld_in",
    "lacertid",
    "lacertid_hd",
    "lacertid_in",
    "leaveallbehind",
    "leaveallbehind_at",
    "leaveallbehind_hd",
    "leaveallbehind_in",
    "letempsperdu",
    "letempsperdu_hd",
    "letempsperdu_in",
    "lifeflashesbeforeweebeyes",
    "lifeflashesbeforeweebeyes_hd",
    "lifeflashesbeforeweebeyes_in",
    "locomotive",
    "locomotive_hd",
    "locomotive_in",
    "luminescence",
    "luminescence_ez",
    "luminescence_in",
    "luminescent",
    "luminescent_hd",
    "luminescent_in",
    "luminousentitieslostheart",
    "luminousentitieslostheart_hd",
    "luminousentitieslostheart_in",
    "lyrith",
    "lyrith_at",
    "lyrith_hd",
    "lyrith_in",
    "magentapotion",
    "magentapotion_hd",
    "magentapotion_in",
    "manifoldhypothesis",
    "manifoldhypothesis_hd",
    "manifoldhypothesis_in",
    "marenol",
    "marenol_hd",
    "marenol_in",
    "mechanted",
    "mechanted_hd",
    "mechanted_in",
    "message",
    "message_at",
    "message_hd",
    "message_in",
    "microwav",
    "microwav_hd",
    "microwav_in",
    "miracleforestvipmix",
    "miracleforestvipmix_ez",
    "miracleforestvipmix_hd",
    "mobilys",
    "mobilys_hd",
    "mobilys_in",
    "modulus",
    "modulus_hd",
    "modulus_in",
    "nexttime",
    "nexttime_hd",
    "nexttime_in",
    "nhelv",
    "nhelv_hd",
    "nhelv_in",
    "nickoftime",
    "nickoftime_hd",
    "nickoftime_in",
    "nighttheater",
    "nighttheater_hd",
    "nighttheater_in",
    "niraicanaimix",
    "niraicanaimix_hd",
    "niraicanaimix_in",
    "nonmelodicragezmugedit",
    "nonmelodicragezmugedit_hd",
    "nonmelodicragezmugedit_in",
    "nooneyesman",
    "nooneyesman_hd",
    "nooneyesman_in",
    "nowisthetimedoit",
    "nowisthetimedoit_hd",
    "nowisthetimedoit_in",
    "nox",
    "nox_hd",
    "nox_in",
    "nwad",
    "nwad_hd",
    "nwad_in",
    "nya",
    "nya_hd",
    "nya_in",
    "onandon",
    "onandon_hd",
    "onandon_in",
    "opia",
    "opia_hd",
    "opia_in",
    "originally",
    "originally_hd",
    "originally_in",
    "orthodox",
    "orthodox_hd",
    "orthodox_in",
    "palescreen",
    "palescreen_hd",
    "palescreen_in",
    "panicparadise",
    "panicparadise_at",
    "panicparadise_hd",
    "panicparadise_in",
    "parallelretrogressiongamever",
    "parallelretrogressiongamever_hd",
    "parallelretrogressiongamever_in",
    "parsey",
    "parsey_hd",
    "parsey_in",
    "petrichor",
    "petrichor_at",
    "petrichor_hd",
    "petrichor_in",
    "pixelrebelz",
    "pixelrebelz_hd",
    "pixelrebelz_in",
    "please",
    "please_at",
    "please_hd",
    "please_in",
    "poisonandoraffection",
    "poisonandoraffection_hd",
    "poisonandoraffection_in",
    "pontdessouvenirs",
    "pontdessouvenirs_hd",
    "pontdessouvenirs_in",
    "poseidon",
    "poseidon_at",
    "poseidon_ez",
    "poseidon_in",
    "pragmatismresurrection",
    "pragmatismresurrection_at",
    "pragmatismresurrection_ez",
    "pragmatismresurrection_in",
    "praw",
    "praw_hd",
    "praw_in",
    "protoflicker",
    "protoflicker_hd",
    "protoflicker_in",
    "quantumhyperspace",
    "quantumhyperspace_hd",
    "quantumhyperspace_in",
    "quovadis",
    "quovadis_hd",
    "quovadis_in",
    "qzkagorequiem",
    "qzkagorequiem_at",
    "qzkagorequiem_hd",
    "qzkagorequiem_in",
    "radiance",
    "radiance_at",
    "radiance_hd",
    "radiance_in",
    "rainyheart",
    "rainyheart_hd",
    "rainyheart_in",
    "rainyseason",
    "rainyseason_hd",
    "rainyseason_in",
    "ramification",
    "ramification_hd",
    "ramification_in",
    "random",
    "random_hd",
    "random_in",
    "realms",
    "realms_hd",
    "realms_in",
    "reanimate",
    "reanimate_hd",
    "reanimate_in",
    "rebirth",
    "rebirth_hd",
    "rebirth_in",
    "reendofadream",
    "reendofadream_ez",
    "reendofadream_hd",
    "reendofadream_in",
    "reimei",
    "reimei_hd",
    "reimei_in",
    "renascencepsystylever",
    "renascencepsystylever_hd",
    "renascencepsystylever_in",
    "resolver",
    "resolver_hd",
    "resolver_in",
    "ressistance",
    "ressistance_hd",
    "ressistance_in",
    "retribution",
    "retribution_hd",
    "retribution_in",
    "retributioncycleofredemption",
    "retributioncycleofredemption_hd",
    "retributioncycleofredemption_in",
    "ripper",
    "ripper_hd",
    "ripper_in",
    "rrharil",
    "rrharil_at",
    "rrharil_hd",
    "rrharil_in",
    "rubbishsorting",
    "rubbishsorting_hd",
    "rubbishsorting_in",
    "satellite",
    "satellite_at",
    "satellite_hd",
    "satellite_in",
    "schadenfreude",
    "schadenfreude_hd",
    "schadenfreude_in",
    "schneid_ez",
    "schneid_hd",
    "secretillumination",
    "secretillumination_ez",
    "secretillumination_in",
    "sein",
    "sein_ez",
    "sein_hd",
    "shadow",
    "shadow_at",
    "shadow_ez",
    "shadow_in",
    "shelter_ez",
    "shelter_hd",
    "shineafter",
    "shineafter_hd",
    "shineafter_in",
    "sigma_ez",
    "sigma_hd",
    "silenceisgoldenspeechisgolden",
    "silenceisgoldenspeechisgolden_ez",
    "silenceisgoldenspeechisgolden_hd",
    "six",
    "six_hd",
    "six_in",
    "slips",
    "slips_at",
    "slips_hd",
    "slips_in",
    "snowdance",
    "snowdance_ez",
    "snowdance_hd",
    "snowdesert",
    "snowdesert_hd",
    "snowdesert_in",
    "solaggyfeatuzumaki",
    "solaggyfeatuzumaki_hd",
    "solaggyfeatuzumaki_in",
    "sparklenewlife",
    "sparklenewlife_hd",
    "sparklenewlife_in",
    "spasmodic",
    "spasmodic_at",
    "spasmodic_hd",
    "spasmodic_in",
    "speedup",
    "speedup_ez",
    "speedup_in",
    "spotlight",
    "spotlight_hd",
    "spotlight_in",
    "starduster",
    "starduster_hd",
    "starduster_in",
    "stardustray",
    "stardustray_at",
    "stardustray_hd",
    "stardustray_in",
    "stasis",
    "stasis_at",
    "stasis_hd",
    "stasis_in",
    "sultanrage",
    "sultanrage_hd",
    "sultanrage_in",
    "surrealism",
    "surrealism_hd",
    "surrealism_in",
    "swingskipdrop",
    "swingskipdrop_hd",
    "swingskipdrop_in",
    "technopolis2085",
    "technopolis2085_at",
    "technopolis2085_hd",
    "technopolis2085_in",
    "temporalshifting",
    "temporalshifting_hd",
    "temporalshifting_in",
    "thechariotreviival",
    "thechariotreviival_hd",
    "thechariotreviival_in",
    "theguru",
    "theguru_ez",
    "theguru_in",
    "themountaineater",
    "themountaineater_hd",
    "themountaineater_in",
    "thewholerest",
    "thewholerest_hd",
    "thewholerest_in",
    "thrashforce",
    "thrashforce_hd",
    "thrashforce_in",
    "timetonightskyfeatleeyujin",
    "timetonightskyfeatleeyujin_hd",
    "timetonightskyfeatleeyujin_in",
    "trane",
    "trane_hd",
    "trane_in",
    "triumphregret",
    "triumphregret_hd",
    "triumphregret_in",
    "trojan",
    "trojan_hd",
    "trojan_in",
    "truehometrueworldrework",
    "truehometrueworldrework_ez",
    "truehometrueworldrework_in",
    "unorthodoxthoughts",
    "unorthodoxthoughts_hd",
    "unorthodoxthoughts_in",
    "upshift",
    "upshift_ez",
    "upshift_in",
    "valorstarcross",
    "valorstarcross_hd",
    "valorstarcross_in",
    "verruckt_ez",
    "verruckt_hd",
    "volcanic",
    "volcanic_ez",
    "volcanic_hd",
    "volcanic_in",
    "water",
    "water_hd",
    "water_in",
    "wavetapper",
    "wavetapper_ez",
    "wavetapper_hd",
    "whatdoyouwantmorethanahappyending",
    "whatdoyouwantmorethanahappyending_at",
    "whatdoyouwantmorethanahappyending_hd",
    "whatdoyouwantmorethanahappyending_in",
    "wilddances",
    "wilddances_hd",
    "wilddances_in",
    "wintercube",
    "wintercube_ez",
    "wintercube_in",
    "withyou",
    "withyou_hd",
    "withyou_in",
    "x12b5d520",
    "x12b5d520_at",
    "x12b5d520_hd",
    "x12b5d520_in",
    "x16bc0a4a",
    "x16bc0a4a_hd",
    "x16bc0a4a_in",
    "x191f17bf",
    "x191f17bf_hd",
    "x191f17bf_in",
    "x1f2096cd",
    "x1f2096cd_at",
    "x1f2096cd_ez",
    "x1f2096cd_in",
    "x1fb07d1f",
    "x1fb07d1f_hd",
    "x1fb07d1f_in",
    "x1fcb9614",
    "x1fcb9614_hd",
    "x1fcb9614_in",
    "x291b6612",
    "x291b6612_hd",
    "x291b6612_in",
    "x30c6dffb",
    "x30c6dffb_at",
    "x30c6dffb_hd",
    "x30c6dffb_in",
    "x3603d4be",
    "x3603d4be_hd",
    "x3603d4be_in",
    "x382b5473",
    "x382b5473_hd",
    "x382b5473_in",
    "x3a6830b6",
    "x3a6830b6_at",
    "x3a6830b6_ez",
    "x3a6830b6_in",
    "x3d13d958",
    "x3d13d958_hd",
    "x3d13d958_in",
    "x46ffe14c",
    "x46ffe14c_hd",
    "x46ffe14c_in",
    "x470c6f09",
    "x470c6f09_hd",
    "x470c6f09_in",
    "x47ac8373",
    "x47ac8373_hd",
    "x47ac8373_in",
    "x47b5e6eb",
    "x47b5e6eb_at",
    "x47b5e6eb_hd",
    "x47b5e6eb_in",
    "x47cfdb7d",
    "x47cfdb7d_hd",
    "x47cfdb7d_in",
    "x4bbedb1d",
    "x4bbedb1d_at",
    "x4bbedb1d_hd",
    "x4bbedb1d_in",
    "x56c1684c",
    "x56c1684c_hd",
    "x56c1684c_in",
    "x72ce8de3",
    "x72ce8de3_ez",
    "x72ce8de3_in",
    "x7419ce5a",
    "x7419ce5a_hd",
    "x7419ce5a_in",
    "x756d6977",
    "x756d6977_hd",
    "x756d6977_in",
    "x7ccefc08",
    "x7ccefc08_hd",
    "x7ccefc08_in",
    "x857f391a",
    "x857f391a_hd",
    "x857f391a_in",
    "x885702eb",
    "x885702eb_hd",
    "x885702eb_in",
    "x8fbc73de",
    "x8fbc73de_hd",
    "x8fbc73de_in",
    "x94f23eea",
    "x94f23eea_hd",
    "x94f23eea_in",
    "x98a3f668",
    "x98a3f668_hd",
    "x98a3f668_in",
    "x9f5bdad3",
    "x9f5bdad3_hd",
    "x9f5bdad3_in",
    "xab7d1994",
    "xab7d1994_hd",
    "xab7d1994_in",
    "xb1a9b7be",
    "xb1a9b7be_hd",
    "xb1a9b7be_in",
    "xb898c723",
    "xb898c723_ez",
    "xb898c723_hd",
    "xbaf3a56c",
    "xbaf3a56c_ez",
    "xbaf3a56c_in",
    "xc26f6edf",
    "xc26f6edf_hd",
    "xc26f6edf_in",
    "xc73d4c5e",
    "xc73d4c5e_hd",
    "xc73d4c5e_in",
    "xc74bf069",
    "xc74bf069_ez",
    "xc74bf069_hd",
    "xc7922b8e",
    "xc7922b8e_at",
    "xc7922b8e_hd",
    "xc7922b8e_in",
    "xcf183754",
    "xcf183754_hd",
    "xcf183754_in",
    "xd8e302e4",
    "xd8e302e4_at",
    "xd8e302e4_hd",
    "xd8e302e4_in",
    "xdd99029b",
    "xdd99029b_hd",
    "xdd99029b_in",
    "xeb38aab6",
    "xeb38aab6_ez",
    "xeb38aab6_hd",
    "xecee02fe",
    "xecee02fe_hd",
    "xecee02fe_in",
    "xef26745b",
    "xef26745b_ez",
    "xef26745b_in",
    "xenolith",
    "xenolith_hd",
    "xenolith_in",
    "xenophobia",
    "xenophobia_hd",
    "xenophobia_in",
    "xepion",
    "xepion_hd",
    "xepion_in",
    "xf9c2121e",
    "xf9c2121e_hd",
    "xf9c2121e_in",
    "xfc5b1df9",
    "xfc5b1df9_hd",
    "xfc5b1df9_in",
    "xff27581d",
    "xff27581d_hd",
    "xff27581d_in",
    "xfyj_ez",
    "xfyj_hd",
    "xing",
    "xing_hd",
    "xing_in",
    "youarethemiserable",
    "youarethemiserable_at",
    "youarethemiserable_ez",
    "youarethemiserable_in",
    "yume_ez",
    "yume_hd",
    "zaban_ez",
    "zaban_hd",
    "yume",
    "70mf",
    "zaban",
    "anotherme0",
    "anotherme0_ez",
    "anotherme0_hd",
    "ark",
    "cross",
    "cleyera",
    "schneid_in",
    "schneid",
    "distort",
    "gungnir",
    "init",
    "random0",
    "random1_ez",
    "random1_hd",
    "random1_in",
    "random2_ez",
    "random2_hd",
    "random3_ez",
    "random3_hd",
    "random3",
    "random4_ez",
    "random4_hd",
    "random4",
    "random5_ez",
    "random5_hd",
    "random5",
    "random6_ez",
    "random6_hd",
    "random6",
    "sigma",
    "shelter",
    "verruckt",
    "xfyj",
)

#: 打包时**排除**的 key：磁盘上有载荷，但不该进包的开发期残留。
#:
#: ★ 这张表是「白名单 → 基准 + 自动收录」改造的配套闸门（2026-11-02）：
#:   `SONGS` 之外、磁盘上又存在的 key 现在会被**自动收录**（游戏更新出新歌就靠这条），
#:   但作者机器上还留着一批实验产物，靠本表挡住，否则它们会被一并打进包。
#:
#: 33 条逐条核实过（读 payload 的 `song` + `chart`，与 `SONGS` 里的条目比对 `(song, diff)`）：
#:   · 别名 26 条 —— 与 `SONGS` 里某条同曲同难度，例 `boundedquietude_in` ≡ `bounded_in`、
#:     `wanju_in` ≡ `xd8e302e4_in`、`adastra_in` ≡ `adastraperaspera_in`、`chrono` ≡ `chronostasis_in`
#:   · 实验变体 4 条 —— `breakover_at4/5/6`、`lhcl_in_off`（同曲同难度，不同参数跑出来的对照）
#:   · 旧格式残留 3 条 —— `inori` / `inori_at` / `70mf_tailfix`，payload 里 `song == ''`
#:     （早期解析还没写歌名的产物，打进去会得到空曲名卡片）
#:
#: ⛔ 这不是「永久黑名单」：游戏正式谱面的 key 由 `song_dir` 派生、带难度后缀或 hash，
#:   不会长成这些名字。真要临时打其中某首，`--keys <key>` 显式指定即可（会打印一行提示）。
EXCLUDE_KEYS: frozenset[str] = frozenset({
    "70mf_tailfix",
    "adastra", "adastra_ez", "adastra_hd", "adastra_in",
    "boundedquietude_in",
    "breakover_at", "breakover_at4", "breakover_at5", "breakover_at6",
    "burn_follow",
    "chrono",
    "credits",
    "gimmick_in",
    "hardcorekwaya_ez",
    "inori", "inori_at",
    "kokoro",
    "kuangxi",
    "lacamp",
    "lhcl_in", "lhcl_in_off",
    "lumi",
    "meikyo",
    "notomorrow",
    "qi_at",
    "qzw_ez", "qzw_in",
    "random1",
    "wanju", "wanju_at", "wanju_in",
    "zhanming",
})


def merge_expected(disk_keys: Iterable[str]) -> list[str]:
    """把「磁盘上真实存在的 key」合并成打包清单。

    顺序 = `SONGS` 原序（**保证旧产物逐位可复现**） + 新曲按 key 排序追加在末尾。
    ★ 追加在末尾还有一个好处：新曲与老曲撞名时，`disambiguate()` 只给新曲加后缀，
      老曲在 manifest 里的 `name` 一个都不变，App 里已有曲目不会突然改名。

    ★ 拆成纯函数是为了能脱离磁盘测：测试直接喂一个假集合，不必造真实 payload
      —— 造文件会写进 assets/，忘了复原就把假曲打进 APK（2026-11-02 自审第 14 条）。
    """
    extra = sorted(set(disk_keys) - set(SONGS) - EXCLUDE_KEYS)
    return list(SONGS) + extra


def expected_keys() -> list[str]:
    """本机**应该**打包的完整清单（磁盘 glob 的薄封装，不含存在性过滤）。"""
    return merge_expected(
        p.stem.removeprefix("touches_") for p in (ROOT / "out").glob("touches_*.json"))

#: 难度等级只登记**有真据**的：bounded_in = IN 16 来自 2026-10-23 真机就绪界面截图。
#: 其余留 None（端内 UI 显示「难度 + 音符数」，不显示假等级）。M3 打包曲库时补真索引。
LEVEL_BY_KEY: dict[str, int | None] = {"bounded_in": 16}

#: `zero=tap` 用的 lead（点击 ▶ → 谱面 t=0 的间隔，ms）。**只有真机测过的曲才登记**。
#:
#: 出处 = `tmp/timeline_<key>*.json` 的 `zero_wall − t0_wall`（不是池里的 d_ideal，
#: 那个是"该用的"D，混了 μ）。bounded_in 三轮实测：
#:   r1 (10-23 22:01) **7471.437** ms  ⇒ verdict=late
#:   r2 (10-23 22:32) **7537.133** ms  ⇒ ok
#:   r3 (10-23 22:54) **7520.086** ms  ⇒ ok（严格 φ：Early 0 / Late 0，1286 音全 P）
#: 取 r3（唯一严格 φ 那轮）。⚠ 三轮跨度 **65.7 ms** ⇒ 这个数**只能当链路验证的起点**，
#:   不能当产品零点（逐轮 D 漂移就有这个量级）；真要精度请用 `zero=onset`。
#:
#: ⛔ 端内曾写死过 `DEFAULT_LEAD_MS = 7479.88`（注释称 bounded_in 专属）。那个数**有出处**：
#:   它是本曲两轮 `d_ideal` 的池中位 (7.44334 + 7.51643)/2 = 7.479885。问题在于
#:   ① 池中位不等于任何一轮的真实 lead（两轮相差 73 ms）；② 真正打 φ 的是 r3 = 7520.086；
#:   ③ 界面从不传 lead ⇒ 它成了**任何曲**的默认值且无曲目校验（跨曲差 300+ ms）。
#:   10-24 删除，改为按曲查表 + 查不到就拒绝（`TapLead.resolve`）。
TAP_LEAD_MS_BY_KEY: dict[str, float | None] = {"bounded_in": 7520.086}


def display_name(song_dir: str) -> str:
    """`BoundedQuietude.FiniteLimitvsSiLiS.0` → `BoundedQuietude`（只做驼峰拆词，不改语义）。"""
    head = song_dir.split(".")[0]
    out = []
    for i, ch in enumerate(head):
        if ch.isupper() and i and not head[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out)


def disambiguate(entries: list[dict]) -> list[dict]:
    """保证 `(name, diff)` 唯一 —— UI 与契约测试都按这个键认曲子。

    ★ 2026-10-25 22:1x：补齐曲库后 **`Random` 七个变体**、**`Another Me` 两首**的
      (显示名, 难度) 撞了，契约测试 `test_songs_have_unique_name_diff_pairs` 直接变红。
      ⛔ 不放宽测试：给重名的加后缀 —— `Random (#3)` / `Another Me (NeutralMoon)`。
      · 同组里"第二段（演唱者段）"全都一样 ⇒ 用第三段（变体号）：`(#0)`…`(#6)`
      · 第二段能区分 ⇒ 用它：`(DAAN)` / `(NeutralMoon)`
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        groups.setdefault((e["name"], e["diff"]), []).append(e)
    for group in groups.values():
        if len(group) <= 1:
            continue
        seconds = {e["song_dir"].split(".")[1] if len(e["song_dir"].split(".")) > 1 else e["song_dir"]
                   for e in group}
        for e in group:
            parts = e["song_dir"].split(".")
            if len(seconds) == 1 and len(parts) > 2:
                tag = f"#{parts[2]}"
            else:
                tag = parts[1] if len(parts) > 1 else e["song_dir"]
            e["name"] = f"{e['name']} ({tag})"
    return entries


def payload_path(key: str) -> Path:
    return ROOT / "out" / f"touches_{key}.json"


def music_length_s(song_dir: str) -> float:
    """母带时长（秒）。优先读 `music.info.json`，缺失则按 WAV 头算，再不行返回 0。"""
    info = ROOT / "samples" / song_dir / "music.info.json"
    if info.is_file():
        try:
            v = float(json.loads(info.read_text(encoding="utf-8")).get("length_s") or 0.0)
            if v > 0:
                return round(v, 3)
        except Exception:
            pass
    wav = music_path(song_dir)
    if not wav.is_file():
        return 0.0
    try:
        import wave as _wave
        with _wave.open(str(wav), "rb") as w:
            n, sr = w.getnframes(), w.getframerate()
        return round(n / float(sr), 3) if sr else 0.0
    except Exception:
        return 0.0


def music_path(song_dir: str) -> Path:
    return ROOT / "samples" / song_dir / "music.wav"


def require_game_canvas(canvas: dict, screen: dict) -> None:
    """canvas 必须是 16:9 的游戏画布。**与端内 `Scheduler.requireGameCanvas` 同一判据**。

    ⛔ 2026-10-24（FACTS §161）：载荷把 canvas 填成 screen（1080x2340）时，端内 `toDevice`
    会把每一个点压进屏幕中间一条竖带 —— 事件照发、日志照常、成绩 40%。端内已加闸门能拒绝
    开火，但那时已经装完机、停好歌了（白跑一趟）。这一层让错误在**打包时**就失败。
    ⛔ 判据不能写成 `canvas != screen`：`touches_schneid/yume` 是 1920x1080==1920x1080 的
    **合法退化**（游戏分辨率恰等于画布），那条判据会把它们判死。
    """
    cw, ch = float(canvas["width"]), float(canvas["height"])
    sw, sh = float(screen["width"]), float(screen["height"])
    if not (cw > 0 and ch > 0):
        raise ValueError(f"canvas 尺寸非法：{cw:g}x{ch:g}")
    lhs, rhs = cw * 9.0, ch * 16.0
    if abs(lhs - rhs) > CANVAS_ASPECT_TOL * max(lhs, rhs):
        raise ValueError(
            f"canvas 口径不对：canvas={cw:g}x{ch:g}（宽高比 {cw / ch:.4f} ≠ 16:9），"
            f"screen={sw:g}x{sh:g} ⇒ 游戏画布固定 16:9（如 1920x1080），"
            "非 16:9 几乎一定是 canvas 被填成了 screen")


def require_events_in_screen(events: list, screen: dict) -> None:
    """事件坐标必须落在 screen 口径内（端内闸门 B 的同一条判据）。"""
    sw, sh = float(screen["width"]), float(screen["height"])
    for i, e in enumerate(events):
        x, y = float(e["x"]), float(e["y"])
        if not (0 <= x <= sw and 0 <= y <= sh):
            raise ValueError(
                f"events[{i}] 坐标 ({x:.1f},{y:.1f}) 不在 screen {sw:g}x{sh:g} 内"
                " ⇒ 载荷口径不对（不是时基问题）")


def slim_payload(src: dict) -> dict:
    """瘦身：只留端内会读的键，事件字段按 `EVENT_KEYS` 白名单。"""
    canvas, screen = src.get("canvas"), src.get("screen")
    events = src.get("events")
    if not isinstance(canvas, dict) or not isinstance(screen, dict) or not isinstance(events, list):
        raise ValueError("载荷缺 canvas / screen / events")
    # ★ 口径闸门（PC 侧孪生）：错在打包时就红，不要等到真机才发现坐标全错
    require_game_canvas(canvas, screen)
    require_events_in_screen(events, screen)
    out_events = []
    for i, e in enumerate(events):
        miss = [k for k in EVENT_KEYS if k not in e]
        if miss:
            # ⛔ 不许跳过或填默认值：缺字段会在端内变成「坐标 0」这种静默错
            raise ValueError(f"events[{i}] 缺字段 {miss}")
        out_events.append({k: e[k] for k in EVENT_KEYS})
    return {
        "song": src.get("song"),
        "chart": src.get("chart"),
        "note_count": src.get("note_count"),
        "canvas": {"width": float(canvas["width"]), "height": float(canvas["height"])},
        "screen": {"width": float(screen["width"]), "height": float(screen["height"])},
        "events": out_events,
    }


# ── 母带包络指纹（S3：把「每曲的 K」变成端内可算）──────────────────────────
ENV_HOP_MS = 10
ENV_MAX_S = 30.0


def env_fingerprint(song_dir: str) -> str:
    """母带前 30 s 的 10 ms 包络 → u8 → base64（≈4 KB/曲）。

    ★ 口径必须与端内 `EnvCodec` **逐字相同**：单声道 (L+R)/2、10 ms 帧、RMS，
      量化 0=数字静音、1..255 覆盖 1..32768 的对数刻度。
      ⛔ 两处各写一套必然分叉，而分叉不会报错 —— 只会让端内解出的 K 偏几十毫秒。
    ★ 为什么是 30 s：标定窗口也是 30 s（倒计时后开始采），曲子一般 7~10 s 后开唱
      ⇒ 最多需要母带的前 ~22 s。取 30 s 留余量，体积可接受。
    """
    import wave  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    wav = music_path(song_dir)
    with wave.open(str(wav), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        n = min(w.getnframes(), int(sr * ENV_MAX_S))
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if ch > 1:
        a = a[: len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
    hop = int(round(sr * ENV_HOP_MS / 1000.0))
    m = len(a) // hop
    rms = np.sqrt((a[: m * hop].reshape(m, hop) ** 2).mean(axis=1))
    log_max = float(np.log(32768.0))
    q = np.zeros(m, dtype=np.int64)
    pos = rms > 0
    q[pos] = np.clip(1 + np.round(np.log(rms[pos]) / log_max * 254.0), 1, 255).astype(np.int64)
    return base64.b64encode(q.astype(np.uint8).tobytes()).decode("ascii")


def silence_ref(song_dir: str) -> dict:
    """母带「静音→有声」边界（秒）。复用生产函数，不另写一份判据。"""
    import audio_round as AR  # noqa: PLC0415  （重型依赖只在需要时导入）

    wav = music_path(song_dir)
    if not wav.exists():
        raise FileNotFoundError(f"缺母带 {wav}（sil_ref 无法预算）")
    rs = AR.reference_silence(str(wav), sustain_ms=AR.SUSTAIN_MS)
    # ★ thr 可能是 None（母带算不出边沿阈值 ⇒ reference_silence 给的是兜底值）。
    #   这是**合法状态**：端内读 null 会用相对判据。以前这里直接 float(None) 崩掉，
    #   只是过去 4 首恰好都有 thr 才没暴露；M3 批量打包时第 5 首就炸了。
    _thr = rs.get("thr")
    return {"sil_ref_s": round(float(rs["sil_ref_s"]), 6),
            "thr": (round(float(_thr), 3) if _thr is not None else None),
            "sustain_ms": float(rs["sustain_ms"]),
            "confirmed": rs.get("confirmed")}


#: **每曲预置 K**（ms）：只在"这首的 K 自动测不出来"时兜底用（`chronostasis_in` 就是这种）。
#:
#: 为什么可以预置：`K` 是**曲目**属性（起音形状决定的检测器偏置），`c` 才是设备属性 ⇒
#: 同一首曲在不同设备上 K 基本一致（G2 只在换设备时作废 K，属于保守策略）。
#: 数值来源只能是"成绩反馈"：例如 10 个音符全判 Late ⇒ 整体偏晚 ~80 ms ⇒ K≈+80。
#: ⚠ 自动标定一旦测出真值会**覆盖**预置值（`CalibMerge` 融合，预置值不参与平均）。
#: ★ 2026-10-28：已被**真机**证明能 AP 的「10 指压力」谱面 ⇒ manifest 里带 `ten_finger_ok=true`，
#: 端内据此**不再**打「结构限制，不是 bug」的警告。
#: 入册条件（缺一不可）：① `pointer_peak >= 9`；② 真机打出 **φ 1,000,000**（有结算截图 + trace 留档）。
#: 加/删条目都要在 `FACTS §189.9` 同步写明日期与证据。
TEN_FINGER_VERIFIED: set[str] = {
    "erabyeconnec10n_at",   # 2026-10-28 φ 1,000,000（1579 音，峰值 9）
    "xbaf3a56c_in",         # 2026-10-28 φ 1,000,000（千紫万紅 IN，1200 音，峰值 9）
    # ★ 2026-10-28 20:18 φ 1,000,000（1306 音，峰值 9）—— 尾部 4 个同位 Flick 靠**第 2 个触点**解决：
    #   一个触点最多只判 3 个同位 Flick；证据见 FACTS §189.17/§189.18 与 out/flick_split_derrichter_2026-10-28.md
    "derrichter_at",
}

K_PRESET_MS: dict[str, float] = {
    # 按**曲名**（= manifest 的 `name`）：K 是曲目属性，同一首的所有难度共享同一个值。
    "Chronostasis": 80.0,
}


def pointer_peak(events: list[dict]) -> int:
    """并发**按下**的峰值（= 这首谱实际用掉几根手指）。

    ★ 2026-10-26 新增：用来标出"10 指压力"的谱面。峰值 ≥9 时调度器会开始抢长按的手指，
      真机表现为偶发纯 Miss（0 Good / 0 Bad）——那是结构限制，不是可调的 bug。
      全库分布（2026-10-26 出货 manifest 实测）：1:10 2:566 3:139 4:291 5:22 6:11 7:8 8:5 9:4（峰值 9）。
    """
    cur: set[int] = set()
    peak = 0
    for e in sorted(events, key=lambda x: (x["t_ms"], 0 if x["action"].endswith("DOWN") else 1)):
        a = e["action"]
        if a in ("DOWN", "POINTER_DOWN"):
            cur.add(e["pointer_id"])
        elif a in ("UP", "POINTER_UP"):
            cur.discard(e["pointer_id"])
        peak = max(peak, len(cur))
    return peak


def blind_note_count(key: str, blind_end_ms: float, song_dir: str = "", diff: str = "") -> Optional[int]:
    """起音盲区里的**音符个数**（不是关键事件数）。

    ★ 2026-10-25 21:2x 新增：旧口径数 `DOWN/POINTER_DOWN/POINTER_UP/UP` 事件，
      同刻双 Flick 拆成两只手后 moonlight 从 2 变 4（而实际只有 2 个音符）⇒ UI 的
      「开头 N 个音符必丢」在撒谎。改成数**音符**。

    ⚠ **必须加回谱面 offset**（2026-10-25 22:2x 修）：`notes_*.json` 的 `t_ms` 是
      **谱面几何时基**，而 `blind_end_ms`（`sil_ref + 0.342`）是**音乐时刻**。
      带 offset 的曲子（全库 24 条，aphasia 高达 +468 ms）不加回就会算错哪个音符落在盲区里。
      口径：`t_ms + offset_sec×1000 ≤ blind_end_ms`（与 `notes_*.json` 的 `t0_rule` 一致）。

    缺 `notes_*.json` 时退回**直接解析谱面**（`pgauto.chart`）：中间件不该是唯一真源，
    否则新克隆的仓库里这一项会静默换成"数事件"的旧口径。
    """
    p = ROOT / "out" / f"notes_{key}.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            ns = d.get("notes") or []
            if ns:
                off_ms = float(d.get("offset_sec") or 0.0) * 1000.0
                return sum(1 for n in ns
                           if float(n.get("t_ms", 1e18)) + off_ms <= blind_end_ms + 1e-6)
        except Exception:
            pass
    return _blind_note_count_from_chart(song_dir, diff, blind_end_ms)


def _blind_note_count_from_chart(song_dir: str, diff: str, blind_end_ms: float) -> Optional[int]:
    """直接从 `out/charts/<song>/<DIFF>.json` 数盲区音符（不依赖 `out/notes_*.json`）。"""
    if not song_dir or not diff:
        return None
    cp = ROOT / "out" / "charts" / song_dir / f"{diff}.json"
    if not cp.is_file():
        return None
    try:
        from pgauto.chart import Chart, load  # noqa: PLC0415
        ch = Chart(load(cp))
        off_ms = float(getattr(ch, "offset", 0.0) or 0.0) * 1000.0
        return sum(1 for n in ch.notes() if n.t_ms + off_ms <= blind_end_ms + 1e-6)
    except Exception:
        return None


def pack_one(key: str, out_root: Path) -> dict:
    src_path = payload_path(key)
    if not src_path.exists():
        raise FileNotFoundError(f"缺载荷 {src_path}")
    raw = src_path.read_bytes()
    src = json.loads(raw.decode("utf-8"))
    song_dir = src.get("song")
    if not song_dir:
        raise ValueError(f"{src_path} 缺 song 字段（无法定位母带）")

    slim = slim_payload(src)
    pdir = out_root / "payload"
    pdir.mkdir(parents=True, exist_ok=True)
    dst = pdir / f"{key}.json"
    # 紧凑分隔符：端内 Json.parsePayload 不关心空白，省 6%
    dst.write_text(json.dumps(slim, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    sil = silence_ref(song_dir)
    n_key = sum(1 for e in slim["events"] if e["action"] in
                ("DOWN", "POINTER_DOWN", "POINTER_UP", "UP"))
    chart = slim.get("chart") or ""
    # ★ 起音盲区：从「音乐起音」到「端内能派发第一个事件」约 0.342 s（cap+检测+L_dev 的不可消部分）。
    #   ⚠ 2026-10-25 21:2x 改口径：这里数的是**音符个数**，不是关键事件数。
    #     旧实现数 `DOWN/POINTER_DOWN/POINTER_UP/UP` 事件 ⇒ 同刻双 Flick 会翻倍
    #     （moonlight 拆成两只手后从 2 变 4，而实际只涉及 2 个音符），UI 的"开头 N 个音符"就撒谎。
    #   ⚠ 而且"盲区必丢 ⇒ 打不出 φ"这条**已被真机证伪**：moonlight IN 的 blind=4 仍打出 φ
    #     （两段锚预派 + rezero 能覆盖盲区）。UI 文案已同步改成"未预派时才会丢"。
    blind_end_ms = (sil["sil_ref_s"] + BLIND_S) * 1000.0
    n_blind_notes = blind_note_count(key, blind_end_ms, song_dir, chart.replace(".json", ""))
    if n_blind_notes is None:
        # 兜底（既没有 notes_<key>.json 也没有谱面）：仍数关键事件，并说清口径不同
        print(f"  ⚠ {key}: 缺 notes/谱面 ⇒ blind_lost 退回「数关键事件」口径（同刻双 Flick 会翻倍）")
        blind_lost = sum(1 for e in slim["events"]
                         if e["action"] in ("DOWN", "POINTER_DOWN", "POINTER_UP", "UP")
                         and e["t_ms"] <= blind_end_ms + 1e-6)
    else:
        blind_lost = n_blind_notes
    return {
        "key": key,
        "name": display_name(song_dir),
        "diff": chart.replace(".json", "").upper() or "?",
        "level": LEVEL_BY_KEY.get(key),
        "song_dir": song_dir,
        "chart": slim.get("chart"),
        "note_count": slim.get("note_count"),
        "n_events": len(slim["events"]),
        "n_key": n_key,
        "sil_ref_s": sil["sil_ref_s"],
        "sil_thr": sil["thr"],
        "sil_confirmed": sil["confirmed"],
        "payload": f"payload/{key}.json",
        "bytes_src": len(raw),
        "bytes_slim": dst.stat().st_size,
        "t_ms_range": [slim["events"][0]["t_ms"], slim["events"][-1]["t_ms"]],
        # null = 本曲没测过 tap 的 lead ⇒ 端内 tap 模式会拒绝（TapLead.resolve）
        "tap_lead_ms": TAP_LEAD_MS_BY_KEY.get(key),
        # 母带包络指纹（S3）：端内标定用它与捕获包络做互相关，算每曲的 K
        "env_b64": env_fingerprint(song_dir),
        "env_hop_ms": ENV_HOP_MS,
        # 首音是否落在盲区（Ⅱ 类 = 非 0）⇒ 端内据此打「⚠ 首音盲区」标记
        "blind_lost": blind_lost,
        # 没测出 K 时的预置值（ms）；缺省不写（旧 manifest 读不到 ⇒ 视为无预置）
        **({"k_preset_ms": K_PRESET_MS[display_name(song_dir)]}
           if display_name(song_dir) in K_PRESET_MS else {}),
        # ★ 2026-10-26：并发**按下**的峰值（这首谱实际用掉几根手指）。
        #   ≥9 ⇒ 调度器会开始"抢正在长按的手指"，真机表现为偶发纯 Miss（0G0B）——
        #   属**结构限制**，不是可调的 bug。全库只有 6 首（`distort_at` 到 10）。
        "pointer_peak": pointer_peak(slim["events"]),
        # ★ 2026-10-28：这首（≥9 指）已被真机证明能 AP ⇒ 端内不打"结构限制"警告。
        #   只有 `TEN_FINGER_VERIFIED` 里的曲目写 true；其余不写（端内默认 false）。
        **({"ten_finger_ok": True} if key in TEN_FINGER_VERIFIED else {}),
        # 母带**总时长**（秒）：课程模式第 2/3 首的下界要用"音乐结束"，不是"末音符"
        "length_s": music_length_s(song_dir),
        "margin_s": round(slim["events"][0]["t_ms"] / 1000.0 - sil["sil_ref_s"], 3),
    }


# ───────────────────────────── 自测 ─────────────────────────────

def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def selftest(entries: list[dict], out_root: Path, timeline_sil: dict[str, float],
             timeline_lead: dict[str, list[float]]) -> None:
    """能抓到真 bug 的检查，不是「跑通就算」。"""
    print("\n=== 自测 ===")
    n_checked = 0
    n_lead = 0
    for e in entries:
        key = e["key"]
        src = json.loads(payload_path(key).read_text(encoding="utf-8"))
        got = json.loads((out_root / e["payload"]).read_text(encoding="utf-8"))

        # ① 事件数一致
        _assert(len(got["events"]) == len(src["events"]),
                f"{key}: 事件数 {len(got['events'])} ≠ {len(src['events'])}")

        # ② 全量逐条字段一致（不是抽检 —— 抽检漏掉的恰是最容易错的尾部）
        for i, (a, b) in enumerate(zip(src["events"], got["events"])):
            for k in REQUIRED_EVENT_KEYS:
                _assert(k in b, f"{key}: events[{i}] 丢了字段 {k}（生产白名单被改窄了？）")
                _assert(a[k] == b[k], f"{key}: events[{i}].{k} {a[k]!r} → {b[k]!r}")
            _assert(set(b) == set(REQUIRED_EVENT_KEYS),
                    f"{key}: events[{i}] 键集合 {sorted(b)} ≠ 期望 {list(REQUIRED_EVENT_KEYS)}")
            n_checked += 1
        _assert(len(got["events"]) == len(src["events"]), f"{key}: 长度在比对中变了")

        # ②b 顶层不许残留端内不读的大块（plan 3 万条就是靠这条挡住的）
        for junk in ("plan", "unassigned", "stats", "constraints", "warnings"):
            _assert(junk not in got, f"{key}: 瘦身后仍带 {junk}（白占了 assets）")

        # ③ 瘦身真的更小（否则说明白名单没生效，plan 还在）
        _assert(e["bytes_slim"] < e["bytes_src"], f"{key}: 瘦身没变小")

        # ④ canvas/screen 保真（端内坐标变换全靠这两个）
        for k in ("canvas", "screen"):
            for d in ("width", "height"):
                _assert(abs(float(got[k][d]) - float(src[k][d])) < 1e-9,
                        f"{key}: {k}.{d} 漂了")

        # ④b 口径闸门：**独立**再查一遍产物 —— 若 slim_payload 里的校验被删掉/绕过，这里红。
        #     保真（④）只保证"没被改写"，不保证"本来就是对的口径"，两者缺一不可。
        try:
            require_game_canvas(got["canvas"], got["screen"])
            require_events_in_screen(got["events"], got["screen"])
        except ValueError as ex:
            _assert(False, f"{key}: 产物口径不对 —— {ex}")

        # ⑤ 关键事件数 > 0 且 ≤ 事件数
        _assert(0 < e["n_key"] <= e["n_events"], f"{key}: n_key={e['n_key']} 不合理")

        # ⑥ sil_ref 落在物理合理区间，且与真机 timeline 记的值一致（有记录时）
        _assert(0.0 <= e["sil_ref_s"] < 30.0, f"{key}: sil_ref={e['sil_ref_s']} 越界")
        if key in timeline_sil:
            d = abs(e["sil_ref_s"] - timeline_sil[key]) * 1000.0
            _assert(d < 1.0, f"{key}: sil_ref 与真机记录差 {d:.2f} ms（应 < 1 ms）")
            print(f"  ✓ {key}: sil_ref {e['sil_ref_s']:.6f}s == 真机 {timeline_sil[key]:.6f}s")
        else:
            print(f"  ✓ {key}: sil_ref {e['sil_ref_s']:.6f}s（无真机记录可比）")

        # ⑦ tap lead：登记了就必须是**某一轮真机实测**出来的（±1 ms），不许编
        lead = e.get("tap_lead_ms")
        if lead is None:
            print(f"  ✓ {key}: tap_lead_ms = null（端内 tap 模式会拒绝本曲，只能用 onset）")
        else:
            _assert(0.0 < lead < 60_000.0, f"{key}: tap_lead_ms={lead} 越界")
            meas = timeline_lead.get(key, [])
            _assert(bool(meas),
                    f"{key}: 登记了 tap_lead_ms={lead}，但 tmp 里没有该曲 timeline 可核对"
                    " ⇒ 无法证明它是实测值（补 timeline，或把表里这项改成 None）")
            d = min(abs(lead - m) for m in meas)
            _assert(d < 1.0,
                    f"{key}: tap_lead_ms={lead} 与任何一轮实测都对不上"
                    f"（最近差 {d:.2f} ms；实测 {[round(m, 3) for m in meas]}）")
            n_lead += 1
            print(f"  ✓ {key}: tap_lead_ms {lead:.3f} == 实测 {[round(m, 3) for m in meas]}")

    print(f"  ✓ 逐条比对 {n_checked} 个事件 × {len(EVENT_KEYS)} 字段，全等")
    print(f"  ✓ tap_lead_ms 已核对 {n_lead} 首（登记了就必然核过；null 的曲端内会拒绝）")


def timeline_leads() -> dict[str, list[float]]:
    """从真机 timeline 取「点击 ▶ → 零点」的实测 lead（ms）= `zero_wall − t0_wall`。

    ⚠ 用 `*_wall` 而不是单调钟那一对：两者在同一轮内只差常数，但 `wall` 两个字段
    是同一次配对写下的，跨钟也不会错。判据与 `tests/test_play_apk_contract.py` 的
    `test_tap_lead_matches_a_measured_timeline` **保持同一个算式**（校验方向相反）。
    ⚠ 取不到时返回空表 —— 那会让「已登记的 lead 必须对上」变成失败而不是静默通过，
      所以空表不是"没得比"，是"登记了却无法证明"。
    """
    out: dict[str, list[float]] = {}
    for f in sorted((ROOT / "tmp").glob("timeline_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        tag, t0, z = d.get("tag"), d.get("t0_wall"), d.get("zero_wall")
        if not tag or not isinstance(t0, (int, float)) or not isinstance(z, (int, float)):
            continue
        base = tag.split("_r")[0] if "_r" in tag else tag
        out.setdefault(base, []).append((float(z) - float(t0)) * 1000.0)
    return out


def verify_java(entries: list[dict], out_root: Path) -> None:
    """最强对照：同一载荷、瘦身前后的 Java 排程输出必须**逐位相同**。

    这比「事件数一样」硬得多 —— 只要有一个字段的值类型或数值变了（例如 int→double），
    坐标取整就会在 .5 边界上漂 1 像素，diff 立刻抓到。
    """
    print("\n=== Java 排程逐位对拍（Scheduler.java）===")
    sched_dir = ROOT / "android/injector/app/src/main/java/io/pgauto/inject/sched"
    # ★ 必须整目录一起编：Scheduler 依赖同包的 Densify（`Json` 是 Scheduler 的内部类）。
    #   只编 Scheduler.java 会 `cannot find symbol` —— 那是编译口径问题，不是载荷问题。
    sched_srcs = sorted(str(p) for p in sched_dir.glob("*.java"))
    jdk = Path("D:/OpenJDK17_17.0.19_10")
    javac, java = jdk / "bin/javac.exe", jdk / "bin/java.exe"
    if not (javac.exists() and java.exists()):
        _assert(shutil.which("javac") is not None, f"找不到 javac（{javac} 不存在且 PATH 里也没有）")
        javac, java = Path("javac"), Path("java")

    with tempfile.TemporaryDirectory() as td:
        cls = Path(td) / "cls"
        cls.mkdir()
        cc = subprocess.run([str(javac), "-nowarn", "-encoding", "UTF-8", "-d", str(cls)] + sched_srcs,
                            capture_output=True)
        if cc.returncode != 0:
            # 把 javac 的真实报错打出来 —— 只抛 CalledProcessError 会把原因吞掉
            raise AssertionError("javac 失败：\n"
                                 + cc.stderr.decode("utf-8", "replace")[:2000])
        for e in entries:
            outs = {}
            for label, path in (("src", payload_path(e["key"])),
                                ("slim", out_root / e["payload"])):
                # ★ 用 bytes 比较，不用 text=True：java 的 stderr 是系统代码页（GBK），
                #   text=True 会让读取线程抛 UnicodeDecodeError（实测会出现，且只打印不中断，
                #   看起来「跑过了」其实解码早就崩了）。stdout 是纯 ASCII TSV，比较 bytes 更严格。
                p = subprocess.run([str(java), "-cp", str(cls),
                                    "io.pgauto.inject.sched.Scheduler",
                                    str(path), "1610", "720", "height"],
                                   capture_output=True)
                if p.returncode != 0:
                    raise AssertionError(
                        f"{e['key']}: Scheduler 退出码 {p.returncode}\n"
                        + p.stderr.decode("utf-8", "replace")[:1500])
                outs[label] = p.stdout
            same = outs["src"] == outs["slim"]
            n = len(outs["src"].splitlines())
            _assert(same, f"{e['key']}: 瘦身前后 Java 排程输出不一致（{n} 行）")
            print(f"  ✓ {e['key']}: {n} 行 TSV 逐字节相同")


def timeline_sil_refs() -> dict[str, float]:
    """从真机 timeline 的 `l4c` 子对象里取回 sil_ref，用来交叉核对预算值。

    ⚠ timeline 顶层没有 sil_ref（它在 `l4c` 里），这里读错位置会静默返回空表、
    让「sil_ref 与真机一致」这条断言变成空转 —— 所以取不到时不算通过，只算没得比。
    """
    out: dict[str, float] = {}
    for f in sorted((ROOT / "tmp").glob("timeline_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        tag = d.get("tag")
        v = (d.get("l4c") or {}).get("sil_ref_s")
        if isinstance(v, (int, float)) and tag:
            base = tag.split("_r")[0] if "_r" in tag else tag
            out.setdefault(base, float(v))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打包端内 assets（M1 S1-a）")
    ap.add_argument("--keys", nargs="*", default=None,
                    help="只打这几首（默认 = 磁盘上有载荷的全部：已知曲按固定顺序 + 新曲按 key 追加）")
    ap.add_argument("--out", default=None, help="输出目录（默认 play app 的 assets）")
    ap.add_argument("--list", action="store_true", help="只列出会打包什么，不写文件")
    ap.add_argument("--verify-java", action="store_true", help="附带 Java 排程逐位对拍")
    ap.add_argument("--no-selftest", action="store_true", help="跳过自测（不建议）")
    args = ap.parse_args()

    out_root = Path(args.out) if args.out else ROOT / "android/play/app/src/main/assets"

    # ★ 2026-11-02：清单不再来自硬编码白名单，而是「SONGS 基准 + 磁盘自动收录 − 排除表」。
    #   游戏更新出新歌后，只要 out/ 里有载荷就会自动进包，不需要改源码。
    expected = expected_keys()
    wanted = expected if args.keys is None else args.keys

    for k in wanted:
        if k in EXCLUDE_KEYS:
            print(f"· {k} 在 EXCLUDE_KEYS（开发期残留）里，本次按显式指定打包")
        elif k not in set(expected):
            print(f"⚠ {k} 不在可打包清单里（磁盘上没有 out/touches_{k}.json？）⇒ 跳过")

    todo = [k for k in wanted if payload_path(k).exists()]
    n_no_payload = len(wanted) - len(todo)
    if n_no_payload > 0 and args.keys is None:
        # 默认清单里会有「SONGS 登记了但本机没解出载荷」的（新用户只解了部分谱面）
        print(f"· 跳过 {n_no_payload} 个磁盘上没有载荷的曲目（没解出来就先跑 tools/extract_chart.py --all）")
    if not todo:
        print("✗ 没有可打包的曲目")
        return 2

    if args.list:
        for k in todo:
            d = json.loads(payload_path(k).read_text(encoding="utf-8"))
            lv = LEVEL_BY_KEY.get(k)
            print(f"  {k:14} {display_name(d.get('song') or '?'):22} "
                  f"{(d.get('chart') or '?').replace('.json', ''):3} "
                  f"Lv {lv if lv is not None else '--':>3}  {d.get('note_count')} 音符"
                  f"  ← {payload_path(k).name}")
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    entries, failed = [], []
    print(f"输出目录：{out_root}")
    for k in todo:
        try:
            e = pack_one(k, out_root)
        except Exception as exc:  # noqa: BLE001
            # ★ 单曲失败不许拖垮全量：M3 是 289 首，任何一首缺母带/缺字段都会让整次打包白跑。
            #   失败清单必须打印出来（不许静默跳过——那会让「291 首」变成实际更少而无人知晓）。
            failed.append((k, f"{type(exc).__name__}: {exc}"))
            print(f"  ✗ {k:14} {type(exc).__name__}: {exc}")
            continue
        entries.append(e)
        print(f"  ✓ {k:14} {e['n_events']:5} 事件（关键 {e['n_key']:5}）"
              f"  {e['bytes_src']/1024:8.0f} KB → {e['bytes_slim']/1024:6.0f} KB"
              f"  sil_ref={e['sil_ref_s']:.4f}s  t_ms {e['t_ms_range'][0]:.0f}~{e['t_ms_range'][1]:.0f}")

    if failed:
        # ★ 2026-11-02：失败必须「可操作」。原实现只打一行 Exception，用户看到
        #   `FileNotFoundError: 缺母带 …` 根本不知道下一步该跑哪条命令。
        print(f"\n✗ {len(failed)} 首打包失败（已完成 {len(entries)} 首）：")
        no_music = [k for k, m in failed if "缺母带" in m]
        no_payload = [k for k, m in failed if "缺载荷" in m]
        rest = [k for k, _m in failed if k not in set(no_music) | set(no_payload)]
        if no_music:
            print(f"  · {len(no_music)} 首缺母带 ⇒ 先跑 tools/extract_music.py --all 再打包")
            print(f"    {' '.join(no_music[:6])}{' …' if len(no_music) > 6 else ''}")
        if no_payload:
            print(f"  · {len(no_payload)} 首缺载荷 ⇒ 先跑 tools/extract_chart.py --all")
        if rest:
            print(f"  · {len(rest)} 首其他异常 ⇒ 往上翻 ✗ 行看具体错误")
        # ★ 失败曲目在 `silence_ref` / `env_fingerprint` 阶段抛错时，payload 已经落盘了
        #   （`pack_one` 先写 dst 再算 sil_ref）。不清理的话，`check_play_apk.py` 的反向检查会报
        #   「发现 N 个 payload 不在 manifest 里（manifest 被截断过？）」—— 提示完全指错方向。
        orphans = []
        for k, _m in failed:
            f = out_root / "payload" / f"{k}.json"
            if f.exists():
                f.unlink()
                orphans.append(k)
        if orphans:
            print(f"  · 已清掉 {len(orphans)} 个失败曲目的残留 payload（否则 APK 校验会误报「manifest 被截断」）")

    # ★ 保证 (显示名, 难度) 唯一：Random 七变体 / Another Me 两首会撞名（2026-10-25 22:1x）
    disambiguate(entries)
    manifest = {
        "version": 1,
        "generated_by": "tools/pack_assets.py",
        # 端内零点公式：zero_dev_ns = onset_ns − sil_ref_s − L_dev_s
        # L_dev 是**设备/路径属性**，不进 manifest（换机要重学），见 plans/2026-10-23-m1-onddevice-play.md §2
        "zero_formula": "zero_dev_ns = onset_ns - sil_ref_s - L_dev_s",
        "songs": entries,
    }
    mpath = out_root / "manifest.json"

    # ★★ 2026-10-28 修：`--keys` 是**子集**打包时，绝不能把 manifest 覆盖成只剩这几首。
    #   修前实测：`--keys derrichter_at` 让 manifest 从 4.85 MB / 1056 首变成 4729 B / 1 首，
    #   而 APK 的 11 组校验**照样全过**（它只查"manifest 可解析 + assets 一致"，不查首数）
    #   ⇒ 这是个**静默**的坑（APK 里只剩一首曲子也能过闸）。
    #   正确行为：子集打包时走**合并** —— 读旧 manifest，只替换本次打的这几条，其余原样保留。
    partial = set(todo) != set(expected)
    if partial and mpath.exists():
        old_doc = json.loads(mpath.read_text(encoding="utf-8"))
        old_songs = old_doc.get("songs") or []
        fresh = {e["key"]: e for e in entries}
        kept = 0
        for i, e in enumerate(old_songs):
            if e.get("key") in fresh:
                old_songs[i] = fresh.pop(e["key"])
                kept += 1
        if fresh:
            # 新登记的曲目（旧 manifest 里没有）追加到末尾
            old_songs.extend(fresh.values())
        manifest["songs"] = old_songs
        print(f"  ⚠ 子集打包（{len(todo)}/{len(expected)}）：manifest 走**合并**模式 "
              f"（替换 {kept} 条 + 新增 {len(fresh)} 条，保留 {len(old_songs) - kept - len(fresh)} 条原样）")
    elif partial:
        print(f"  ⚠ 子集打包（{len(todo)}/{len(expected)}）且没有旧 manifest ⇒ "
              f"只能写 {len(entries)} 首，**出包前必须补全**")

    # ★ newline="\n" 必须显式写：Windows 上 write_text 默认把 \n 转成 \r\n，
    #   会让 manifest 每次打包都整文件 CRLF 化（git 又要按 .gitattributes 归一化，白白产生巨大 diff）。
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                     encoding="utf-8", newline="\n")
    n_songs = len(manifest["songs"])
    print(f"  ✓ manifest.json（{mpath.stat().st_size} B，{n_songs} 首）")
    # ★ 2026-11-02：判据从「不等于」改成「少于」。
    #   原写法在「显式 --keys 打了一首排除表里的曲目」时会误杀：合并后首数 1057 > 1056，
    #   而 1057 != 1056 直接拒绝出包，可错误信息看起来却像 manifest 被写坏了。
    #   真正要防的只有「合并失败导致条目变少」，所以只查 <（方案自审第 13 条）。
    if partial and n_songs < len(expected):
        print(f"  ✗ manifest 首数 {n_songs} < 应打包的 {len(expected)} ⇒ **拒绝出包**（合并失败？补全后再出包）")
        return 3
    if n_songs > len(expected):
        print(f"  · manifest 首数 {n_songs} > 应打包的 {len(expected)}（多半是显式打了排除表里的曲目）")

    if not args.no_selftest:
        selftest(entries, out_root, timeline_sil_refs(), timeline_leads())
        if args.verify_java:
            verify_java(entries, out_root)

    if failed:
        # ★ 不许静默降级：原有设计是「单曲失败不拖垮全量」（产物仍可用），这没错；
        #   但把退出码也留在 0，就等于告诉调用方「一切正常」——
        #   build.sh 拿它出包会得到一个少了几首、却没人知道的 manifest。
        #   所以：继续打完其余曲目（不中断），最后用非 0 退出码表明「本次不完整」。
        print(f"\n✗ 本次产物不完整（{len(failed)} 首失败）⇒ 修好后再出包，别拿它 build")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
