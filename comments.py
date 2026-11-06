
"""
评语池：三段组合，36x36x36 = 46656 条唯一组合。

多账号并发时共用一个实例（CommentPool.shared），内部加锁 + 已用集合落盘，
保证跨线程、跨进程重启都不会给出重复评语。
"""

import json
import os
import pathlib
import random
import threading

OPEN = [
    "老师讲课条理清晰",
    "老师备课非常认真",
    "老师课堂节奏把握得很好",
    "老师讲课重点突出",
    "老师对课程内容非常熟悉",
    "老师讲课深入浅出",
    "老师授课思路清楚",
    "老师讲课很有感染力",
    "老师对知识点讲解细致",
    "老师每节课都准备得很充分",
    "老师讲课逻辑性强",
    "老师善于把抽象的内容讲明白",
    "老师授课态度认真负责",
    "老师课堂上讲解耐心",
    "老师讲课时条理分明",
    "老师对课程重点把握准确",
    "老师讲课风格亲切",
    "老师讲解例题很到位",
    "老师课堂组织得很好",
    "老师讲课时间分配合理",
    "老师讲课不照本宣科",
    "老师对难点讲解很透彻",
    "老师讲课声音清楚",
    "老师授课内容充实",
    "老师讲课很有耐心",
    "老师对课堂的把控很好",
    "老师讲课层次分明",
    "老师讲解概念非常清楚",
    "老师课堂讲授非常认真",
    "老师备课细致",
    "老师讲课思路连贯",
    "老师授课认真负责",
    "老师对课程结构安排合理",
    "老师讲课通俗易懂",
    "老师课堂讲解细致入微",
    "老师对教学内容非常熟练",
]

BODY = [
    "作业和练习的布置很有针对性",
    "课后答疑很及时",
    "批改作业很认真",
    "课堂上会留出提问的时间",
    "对同学的问题都耐心解答",
    "课程资料的整理很完整",
    "实验和上机环节安排合理",
    "课件做得清楚易懂",
    "会结合实例讲解知识点",
    "经常提醒我们注意易错点",
    "和同学的互动比较多",
    "课堂氛围比较轻松",
    "进度安排和大家的接受程度比较匹配",
    "会主动关心跟不上的同学",
    "复习时给出的重点很明确",
    "考试范围和讲课内容对应得很好",
    "课程要求讲得很清楚",
    "给的学习建议很实用",
    "课堂练习的设计有帮助",
    "遇到问题时能得到及时回应",
    "对课程作业的反馈很具体",
    "会补充一些课本外的内容",
    "讲授时注意前后知识的衔接",
    "对基础薄弱的同学也比较关照",
    "课堂上的举例贴近实际",
    "布置的任务量比较合适",
    "会提醒大家做好课前预习",
    "对课程难点会反复强调",
    "课堂上会带大家梳理知识框架",
    "课后提供的参考资料比较有用",
    "对同学的疑问回复得很详细",
    "讲课过程中会关注大家的反应",
    "课程安排整体比较紧凑合理",
    "会结合往届情况给出提醒",
    "对作业中的共性问题会集中讲评",
    "课堂纪律要求明确但不苛刻",
]

TAIL = [
    "这门课让我收获很大",
    "感觉学到了很多实用的东西",
    "对这门课的整体印象很好",
    "希望以后还能上这位老师的课",
    "课程内容对我帮助很大",
    "这门课让我对专业有了更多了解",
    "整体体验很不错",
    "学完之后思路清楚了很多",
    "这门课值得认真对待",
    "很庆幸选到了这门课",
    "课程安排让人比较满意",
    "对这门课的评价很高",
    "这门课的内容安排得很扎实",
    "上完这门课感觉比较充实",
    "希望课程能继续保持这样的水平",
    "这门课让我对这个方向产生了兴趣",
    "整体上是一段不错的经历",
    "课程的质量超出我的预期",
    "这门课的学习体验比较好",
    "从中获得的知识比较扎实",
    "这门课让我打下了不错的基础",
    "学习过程比较顺利",
    "课程内容组织得比较系统",
    "这门课对我后续的学习有帮助",
    "整体感受比较正面",
    "这门课让我改进了学习方法",
    "课程结束后还愿意继续了解相关内容",
    "这门课的价值比较高",
    "学习的获得感比较强",
    "这门课让我对这个领域有了新的认识",
    "课程的学习曲线安排得比较友好",
    "这门课的内容比较有启发性",
    "整体来说比较推荐这门课",
    "这门课的教学效果不错",
    "学完感觉比较有收获",
    "这门课让我更有信心继续深入",
]

class CommentPool:
    """组合去重器。容量 = 36*36*36 = 46656 条唯一评语。"""

    CAPACITY = len(OPEN) * len(BODY) * len(TAIL)

    _singleton = None
    _singleton_lock = threading.Lock()

    @classmethod
    def shared(cls, store_path=None, seed=None):
        """进程内唯一的池子：所有账号线程共用，避免互相撞评语。"""
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls(seed=seed, store_path=store_path)
            return cls._singleton

    def __init__(self, seed=None, store_path=None):
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.store = pathlib.Path(store_path) if store_path else None
        self.used = set()
        self._dirty = 0
        self.overflow = 0
        if self.store and self.store.exists():
            try:
                data = json.loads(self.store.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.used = set(data)
            except Exception:
                self.used = set()

    def _make(self):
        return "%s\uff0c%s\uff0c%s\u3002" % (
            self.rng.choice(OPEN),
            self.rng.choice(BODY),
            self.rng.choice(TAIL),
        )

    def next(self):
        with self.lock:
            s = None
            for _ in range(6000):
                cand = self._make()
                if cand not in self.used:
                    s = cand
                    break
            if s is None:

                while True:
                    self.overflow += 1
                    base = "%s\uff0c%s\uff0c%s" % (
                        self.rng.choice(OPEN), self.rng.choice(BODY), self.rng.choice(TAIL))
                    s = "%s\uff08\u7b2c%d\u6761\uff09\u3002" % (base, self.overflow)
                    if s not in self.used:
                        break

            self.used.add(s)
            self._dirty += 1
            if self._dirty >= 5:
                self._flush()
            return s

    def _flush(self):
        if not self.store:
            return
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self.used), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.store)
            self._dirty = 0
        except Exception:
            pass

    def flush(self):
        with self.lock:
            self._flush()

    def stats(self):
        with self.lock:
            return {"used": len(self.used), "capacity": self.CAPACITY}

AFFIRM = [
    "老师备课认真，讲解思路清晰",
    "老师上课富有激情，能带动课堂气氛",
    "课程内容充实，案例贴近实际",
    "老师答疑耐心，反馈及时",
    "老师对课堂节奏把控得当",
    "课程知识框架清晰，容易跟上",
    "老师讲解细致，重点突出",
    "老师课后辅导到位",
    "课程资料齐全，参考价值高",
    "老师教学经验丰富，例题典型",
    "老师治学严谨，要求明确",
    "课程理论与实践结合得好",
    "老师善于启发思考",
    "课堂互动形式多样",
    "老师对新技术、新案例引入及时",
    "老师批改作业认真细致",
    "课程进度安排合理",
    "老师为人亲和，容易沟通",
    "课程体系完整，内容由浅入深",
    "老师责任心强，从不敷衍",
    "老师会用多种方式帮助理解难点",
    "课程考核方式公平合理",
    "老师鼓励提问，氛围开放",
    "老师结合工程经验讲解，收获大",
    "老师板书和课件都条理清楚",
    "老师关注每个同学的学习状态",
    "课程内容与培养目标契合",
    "老师推荐的学习资源很有用",
    "老师实训指导专业",
    "老师重视培养我们的自学能力",
    "老师课堂管理井然有序",
    "老师讲解由浅入深，循序渐进",
]

IMPROVE = [
    "希望增加课堂互动和讨论环节",
    "希望能多结合实际工程案例讲解",
    "希望课后作业的讲解能更详细一些",
    "希望增加一些动手实践的机会",
    "希望重难点部分能再放慢节奏",
    "希望能补充更多课外拓展资料",
    "希望增加阶段性小测验帮助巩固",
    "希望课堂提问的形式能更多样",
    "希望理论部分能配更多演示实验",
    "希望能增加小组合作学习的环节",
    "希望对课后疑问的回复能再快一些",
    "希望能引入更多行业前沿内容",
    "希望课件能提前发布方便预习",
    "希望增加线上答疑的渠道",
    "希望复习串讲能更系统一些",
    "希望能增加与往届学长的经验分享",
    "希望习题课的课时能再多一点",
    "希望课程项目的要求说明能更清晰",
    "希望能增加过程性考核的比重说明",
    "希望重难点的练习题能再多一些",
    "希望老师上课语速偶尔能慢一点",
    "希望能增加课程相关的竞赛引导",
    "希望实验环节的时间能更充裕",
    "希望对基础薄弱的同学多一些辅导",
    "希望课堂案例能再丰富一些",
    "希望能增加翻转课堂的尝试",
    "希望期末复习的重点能更明确",
    "希望能增加课程思政与专业的结合案例",
    "希望对小组作业的分工指导更细",
    "希望课程平台的资源能再及时更新",
    "希望能多讲讲知识点的实际应用场景",
    "希望能增加期中反馈的环节",
]

class GcPool:
    """过程评价的肯定/改进方向池，各自独立去重并落盘。"""

    def __init__(self, store_path=None):
        self.store = pathlib.Path(store_path) if store_path else None
        self.affirm_used = set()
        self.improve_used = set()
        self.rng = random.Random()
        self._dirty = 0
        self._load()

    def _load(self):
        if self.store and self.store.exists():
            try:
                data = json.loads(self.store.read_text(encoding="utf-8"))
                self.affirm_used = set(data.get("affirm") or [])
                self.improve_used = set(data.get("improve") or [])
            except Exception:
                pass

    def _pick(self, pool, used):
        for _ in range(2000):
            c = self.rng.choice(pool)
            if c not in used:
                return c
        return self.rng.choice(pool) + "。"

    def _flush(self):
        if not self.store:
            return
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "affirm": sorted(self.affirm_used),
                "improve": sorted(self.improve_used),
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.store)
        except Exception:
            pass

    def next_affirm(self):
        c = self._pick(AFFIRM, self.affirm_used)
        self.affirm_used.add(c)
        self._dirty += 1
        if self._dirty >= 5:
            self._flush()
        return c

    def next_improve(self):
        c = self._pick(IMPROVE, self.improve_used)
        self.improve_used.add(c)
        self._dirty += 1
        if self._dirty >= 5:
            self._flush()
        return c

    def flush(self):
        self._flush()
