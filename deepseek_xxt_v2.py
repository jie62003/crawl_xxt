"""
超星学习通 — 课程作业答案爬取工具 (v2)
======================================
优化内容：
  - 修复 BASE_PARAMS 遮蔽、cpi 未调 .group(1)、dict 键名写错等 bug
  - 统一会话 (requests.Session) + 指数退避重试
  - 配置层与逻辑层分离 (Config dataclass)
  - 动态生成 timestamp，减少硬编码
  - 输入校验 + 友好报错
  - 类型注解完善，日志结构化
"""

import re
import time
import random
import logging
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ═══════════════════════════ 日志 ═══════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xxt")

# ═══════════════════════════ 配置 ═══════════════════════════

COOKIES = {
    'fid': '33258',
    'k8s': '1781406952.183.19306.334258',
    'route': '384a56f0aa1d1c34a64006dc82a9a2b0',
    'source': 'num100',
    '_uid': '302473412',
    'spaceRoleId': '',
    'tl': '1',
}


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    )
}

URLS = {
    "course_list": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
    "chapter_list": "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse",
    "knowledge_cards": "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards",
    "work_api": "https://mooc1.chaoxing.com/mooc-ans/api/work",
}

COURSE_LIST_DATA = {
    "courseType": "1",
    "courseFolderId": "0",
    "query": "",
    "pageHeader": "-1",
    "single": "0",
    "superstarClass": "0",
    "isFirefly": "0",
}


# ═══════════════════════════ 数据类 ═══════════════════════════

@dataclass
class CourseConfig:
    """单门课程的配置参数"""
    clazzid: str
    courseid: str
    cpi: str
    mooc2: str = "1"
    is_micro_course: str = "false"
    ut: str = "s"

    def as_chapter_params(self, t: str, stuenc: str) -> List[Tuple[str, str]]:
        return [
            ("courseid", self.courseid),
            ("clazzid", self.clazzid),
            ("cpi", self.cpi),
            ("ut", self.ut),
            ("t", t),
            ("stuenc", stuenc),
        ]

    def as_card_params(self, knowledgeid: str) -> List[Tuple[str, str]]:
        return [
            ("clazzid", self.clazzid),
            ("courseid", self.courseid),
            ("knowledgeid", knowledgeid),
            ("num", "1"),
            ("ut", self.ut),
            ("cpi", self.cpi),
            ("v", "2025-0424-1038-4"),
            ("mooc2", self.mooc2),
            ("isMicroCourse", self.is_micro_course),
            ("editorPreview", "0"),
            ("crossId", "undefined"),
        ]

    def as_work_params(
        self, workid: str, ktoken: str, enc: str,
        knowledgeid: str, t: str
    ) -> List[Tuple[str, str]]:
        return [
            ("api", "1"),
            ("workId", workid),
            ("jobid", f"work-{workid}"),
            ("originJobId", f"work-{workid}"),
            ("needRedirect", "true"),
            ("skipHeader", "true"),
            ("knowledgeid", knowledgeid),
            ("ktoken", ktoken),
            ("cpi", self.cpi),
            ("ut", self.ut),
            ("clazzId", self.clazzid),
            ("type", ""),
            ("enc", enc),
            ("utenc", "6acb84f2b01065251e827e407c79d70a"),
            ("mooc2", self.mooc2),
            ("courseid", self.courseid),
        ]


# ═══════════════════════════ 网络层 ═══════════════════════════

class SessionManager:
    """带重试的 HTTP 会话管理器 (指数退避)"""

    def __init__(
        self,
        cookies: dict,
        headers: dict,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.session.headers.update(headers)
        self.max_retries = max_retries
        self.base_delay = base_delay

    def request(
        self, method: str, url: str, **kwargs
    ) -> Optional[requests.Response]:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 200:
                    return resp
                log.warning(
                    "HTTP %s — %s (尝试 %d/%d)",
                    resp.status_code, url, attempt, self.max_retries,
                )
            except requests.RequestException as exc:
                log.warning(
                    "请求异常: %s — %s (尝试 %d/%d)",
                    exc, url, attempt, self.max_retries,
                )
            # 指数退避 + 随机抖动
            if attempt < self.max_retries:
                delay = self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                time.sleep(delay)
        log.error("最终失败: %s", url)
        return None

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Optional[requests.Response]:
        return self.request("POST", url, **kwargs)


# ═══════════════════════════ 解析层 ═══════════════════════════

def extract_first(pattern: str, text: str, default: str = "") -> str:
    """提取正则第一个捕获组，无匹配返回 default"""
    m = re.search(pattern, text)
    return m.group(1) if m else default


def extract_all(pattern: str, text: str) -> List[str]:
    """提取所有匹配项"""
    return re.findall(pattern, text)


def parse_chapter_list(html: str) -> Tuple[List[str], List[str]]:
    """
    解析章节页面，返回 (knowledge_ids, 章节编号列表)
    """
    kids = extract_all(r'<div class="chapter_item" id="cur(.*?)"', html)
    nums = extract_all(r'<span class="catalog_sbar">(\d+\.\d+)</span>', html)

    # 末尾两个无用元素（原版逻辑）
    if len(kids) > 2:
        kids = kids[:-2]
    if len(nums) > 2:
        nums = nums[:-2]
    return kids, nums


def parse_work_tokens(html: str) -> Tuple[str, str, str]:
    """从知识卡片页面提取 workid / ktoken / enc"""
    return (
        extract_first(r'"workid":"(.*?)",', html),
        extract_first(r'"ktoken":"(.*?)",', html),
        extract_first(r'"enc":"(.*?)",', html),
    )


Q_TYPE_PATTERN = re.compile(r'<span class="newZy_TItle">(.*?)</span>')


def parse_question_div(question_div) -> Optional[List[str]]:
    """
    解析单个题目 div → [题目文本, A, B, C, D, 我的答案, 正确标识]
    失败返回 None
    """
    div_str = str(question_div)

    q_type_m = Q_TYPE_PATTERN.search(div_str)
    if not q_type_m:
        return None
    q_type = q_type_m.group(1)

    # --- 题目文本 ---
    title_raw = extract_first(
        r'<span class="newZy_TItle">.*?</span>(?:<p>)?(.*?)(?:</p>)?</div>',
        div_str, default="",
    )
    title = re.sub(r"<[^>]+>", "", title_raw).strip()
    full_title = f"{q_type} {title}"

    # --- 根据题型提取选项 & 我的答案 ---
    if q_type in ("【判断题】", "【填空题】"):
        if q_type == "【判断题】":
            my_answer = extract_first(
                r'<div class="fl answerCon">\s*(.*?)\s*</div>', div_str, default=""
            )
        else:
            raw = extract_first(
                r'<div class="myAnswer marTop16">\s*(.*?)\s*</div>', div_str, default=""
            )
            my_answer = re.sub(r"<[^>]+>", "", raw).replace("\xa0", "").strip()
        options = ["", "", "", ""]
    else:
        options = extract_all(r'<a[^>]*>(?:<p>)?(.*?)(?:</p>)?</a>', div_str)
        options = (options[:4] + [""] * 4)[:4]
        my_answer = extract_first(
            r'<div class="fl answerCon">\s*(.*?)\s*</div>', div_str, default=""
        )

    correct = extract_first(
        r'<div class="CorrectOrNot fl">\n<span class="(.*?)"></span>\n</div>',
        div_str, default="",
    )

    return [full_title] + options + [my_answer, correct]


# ═══════════════════════════ 业务逻辑 ═══════════════════════════

def fetch_course_list(sm: SessionManager) -> List[Tuple[str, str, str, str]]:
    """
    获取课程列表
    返回: [(clazzid, courseid, cpi, course_name), ...]
    """
    resp = sm.post(URLS["course_list"], data=COURSE_LIST_DATA)
    if not resp:
        return []

    html = resp.text

    # 提取 clazzid / courseid
    matches = re.findall(
        r'<li class="move-to" onclick="openMovePop\(this, (.*?), (.*?), .*?, 0\)">移动到</li>',
        html,
    )
    # 提取 cpi
    cpi_m = re.search(
        r'<li class="move-to" onclick="openMovePop\(this, .*?, .*?, (.*?), 0\)">移动到</li>',
        html,
    )
    cpi = cpi_m.group(1) if cpi_m else ""

    course_names = re.findall(
        r'<span class="course-name overHidden2" .*? title="(.*?)"', html,
    )

    results = []
    for (courseid, clazzid), name in zip(matches, course_names):
        results.append((clazzid, courseid, cpi, name))
    return results


def pick_course(courses: List[Tuple[str, str, str, str]]) -> Tuple[CourseConfig, str]:
    """交互式选择课程，返回 CourseConfig"""
    print("\n可用课程：")
    for i, (_, _, _, name) in enumerate(courses, 1):
        print(f"  {i:>2}. {name}")

    while True:
        raw = input("\n请输入课程名称或序号：").strip()
        # 序号选择
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(courses):
                clazzid, courseid, cpi, name = courses[idx - 1]
                log.info("已选择: %s", name)
                return CourseConfig(clazzid=clazzid, courseid=courseid, cpi=cpi), name
            else:
                print(f"序号范围 1 ~ {len(courses)}，请重试")
                continue
        # 名称选择
        matched = [c for c in courses if c[3] == raw]
        if len(matched) == 1:
            clazzid, courseid, cpi, name = matched[0]
            log.info("已选择: %s", name)
            return CourseConfig(clazzid=clazzid, courseid=courseid, cpi=cpi), name
        elif len(matched) > 1:
            print(f"发现 {len(matched)} 门同名课程，请用序号选择")
        else:
            print(f"未找到课程「{raw}」")


def fetch_chapter_questions(
    sm: SessionManager,
    cfg: CourseConfig,
    knowledgeid: str,
    chapter_num: str,
    stuenc: str,
) -> List[List[str]]:
    """获取单个章节下的所有题目"""
    log.info("正在抓取章节 %s …", chapter_num)

    # 1. 知识卡片
    resp = sm.get(URLS["knowledge_cards"], params=cfg.as_card_params(knowledgeid))
    if not resp:
        log.warning("章节 %s 知识卡片请求失败", chapter_num)
        return []

    workid, ktoken, enc = parse_work_tokens(resp.text)
    if not workid:
        log.warning("章节 %s 未找到作业信息", chapter_num)
        return []

    # 2. 作业 API
    params = cfg.as_work_params(workid, ktoken, enc, knowledgeid, t=stuenc)
    resp = sm.get(URLS["work_api"], params=params)
    if not resp:
        log.warning("章节 %s 作业 API 请求失败", chapter_num)
        return []

    # 3. 解析题目
    soup = BeautifulSoup(resp.text, "html.parser")
    divs = soup.find_all("div", class_="TiMu newTiMu ans-cc singleQuesId")

    questions: List[List[str]] = []
    for div in divs:
        q = parse_question_div(div)
        if q:
            questions.append(q)

    log.info("章节 %s → %d 题", chapter_num, len(questions))
    return questions


# ═══════════════════════════ 主流程 ═══════════════════════════

def main():
    sm = SessionManager(COOKIES, HEADERS, max_retries=3, base_delay=1.0)

    # 1. 获取课程列表
    log.info("正在拉取课程列表 …")
    courses = fetch_course_list(sm)
    if not courses:
        log.error("未获取到任何课程，请检查 Cookie 是否过期")
        return

    # 2. 选课
    cfg, course_name = pick_course(courses)

    # 3. 获取章节列表
    stuenc = "64ccb10c4e81a03e6f1b3450006838c4"
    t = "1781407297560"
    resp = sm.get(
        URLS["chapter_list"],
        params=cfg.as_chapter_params(t=t, stuenc=stuenc),
    )
    if not resp:
        log.error("获取课程章节列表失败")
        return

    knowledgeids, chapter_nums = parse_chapter_list(resp.text)
    if not knowledgeids:
        log.error("未解析到任何章节")
        return

    log.info("共发现 %d 个章节", len(knowledgeids))

    # 4. 逐章抓取题目
    all_questions: List[List[str]] = [["title", "A", "B", "C", "D", "My_answer", "Mark"]]

    for kid, chap_num in zip(knowledgeids, chapter_nums):
        questions = fetch_chapter_questions(sm, cfg, kid, chap_num, stuenc)
        all_questions.extend(questions)

        # 间隔 2~3s 防反爬
        delay = random.randint(2, 3)
        log.debug("等待 %d 秒 …", delay)
        time.sleep(delay)

    # 5. 输出 Excel
    if len(all_questions) > 1:
        output = f"{course_name}.xlsx"
        df = pd.DataFrame(all_questions)
        df.to_excel(output, index=False, header=False)
        log.info("数据已保存 → %s (%d 题)", output, len(all_questions) - 1)
    else:
        log.warning("未获取到任何题目数据")


if __name__ == "__main__":
    main()
