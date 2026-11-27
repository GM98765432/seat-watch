"""KBS교향악단 베토벤 9번 '합창' 취소표 감시.

야놀자(NOL) 잔여석 API를 조회해서 좌석이 새로 생기면
카카오톡 '나에게 보내기'로 알린다. 표준 라이브러리만 사용한다.

환경 변수 (GitHub Secrets):
  KAKAO_REST_API_KEY, KAKAO_CLIENT_SECRET, KAKAO_REFRESH_TOKEN
옵션:
  --test   현재 잔여석을 담은 테스트 메시지를 무조건 보낸다.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).with_name("state.json")

# 좌석 우선순위 (사용자 지정): B석 > A석 > S석 > C석. R석은 감시하지 않음.
PRIORITY = ["B석", "A석", "S석", "C석"]

# 같은 등급에 이 수 이상 남아 있을 때만 알린다 (연석 가능성이 있는 경우만).
# 잔여석 API는 등급별 개수만 주므로, 실제로 붙어 있는지는 예매 화면에서 확인해야 한다.
MIN_SEATS = 2

PERFORMANCES = [
    {
        "goods": "26014713",
        "label": "12/24(목) 20:00 KBS교향악단 70주년 송년음악회 〈합창〉",
        "starts": datetime(2026, 12, 24, 20, 0, tzinfo=KST),
        "sac_url": "https://www.sac.or.kr/site/main/show/show_view?SN=81233",
    },
    {
        "goods": "25017207",
        "label": "12/30(수) 20:00 KBS교향악단 제833회 정기연주회 〈합창〉",
        "starts": datetime(2026, 12, 30, 20, 0, tzinfo=KST),
        "sac_url": "https://www.sac.or.kr/site/main/show/show_view?SN=74852",
    },
]

SEATS_API = "https://nol.yanolja.com/ticket/products/api/remaining-seats?goodsCode={goods}&playSeq=001"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def http(url, data=None, headers=None, timeout=20):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={**HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_seats(goods):
    data = http(SEATS_API.format(goods=goods))
    return {s["seatGradeName"]: int(s["remainCnt"]) for s in data.get("remainSeat", [])}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seats": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def kakao_send(text, link_url):
    key = os.environ.get("KAKAO_REST_API_KEY")
    secret = os.environ.get("KAKAO_CLIENT_SECRET")
    refresh = os.environ.get("KAKAO_REFRESH_TOKEN")
    if not (key and refresh):
        print("[kakao] Secrets가 없어 메시지를 보내지 않음:\n" + text)
        return False
    token_req = {"grant_type": "refresh_token", "client_id": key, "refresh_token": refresh}
    if secret:
        token_req["client_secret"] = secret
    access = http("https://kauth.kakao.com/oauth/token", token_req)["access_token"]
    template = {
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": "예술의전당 예매",
    }
    result = http(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        {"template_object": json.dumps(template, ensure_ascii=False)},
        headers={"Authorization": f"Bearer {access}"},
    )
    ok = result.get("result_code") == 0
    print(f"[kakao] 전송 {'성공' if ok else '실패'}: {result}")
    return ok


def alarm_send(title, text, link_url):
    """Pushover 긴급(emergency) 알림: 무음·방해금지 모드에서도 울리고, 확인할 때까지 반복한다.

    아이폰 Pushover 앱 설정에서 '긴급 우선순위 Critical Alerts'를 켜 두어야 한다.
    """
    token = os.environ.get("PUSHOVER_APP_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not (token and user):
        print("[alarm] Pushover Secrets가 없어 알람을 보내지 않음")
        return False
    result = http(
        "https://api.pushover.net/1/messages.json",
        {
            "token": token,
            "user": user,
            "title": title,
            "message": text[:1000],
            "priority": 2,        # 긴급: 확인 버튼을 누를 때까지 반복
            "retry": 30,          # 30초마다 다시 울림
            "expire": 1800,       # 최대 30분 동안
            "sound": "persistent",
            "url": link_url,
            "url_title": "예술의전당 예매 페이지",
        },
    )
    ok = result.get("status") == 1
    print(f"[alarm] 전송 {'성공' if ok else '실패'}: {result}")
    return ok


def main():
    test_mode = "--test" in sys.argv
    now = datetime.now(KST)
    active = [p for p in PERFORMANCES if now < p["starts"] - timedelta(hours=1)]
    if not active and not test_mode:
        print("모든 공연의 예매가 마감되어 감시를 종료합니다.")
        return

    state = load_state()
    current, errors = {}, []
    for p in active or PERFORMANCES:
        try:
            current[p["goods"]] = fetch_seats(p["goods"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{p['label']}: {exc}")

    for p in PERFORMANCES:
        if p["goods"] in current:
            seats = current[p["goods"]]
            print(f"{p['label']} | " + ", ".join(f"{g} {seats.get(g, 0)}" for g in PRIORITY))

    if errors:
        # 카카오톡은 좌석이 생겼을 때만 보낸다. 조회 실패는 Actions 기록에만 남긴다.
        print("조회 실패:\n" + "\n".join(errors))

    # 새로 생긴 좌석만 알린다 (이전보다 늘어난 등급).
    lines, link, any_gained = [], None, False
    for p in PERFORMANCES:
        seats = current.get(p["goods"])
        if seats is None:
            continue
        prev = state["seats"].get(p["goods"], {})
        gained = [
            g for g in PRIORITY
            if seats.get(g, 0) >= MIN_SEATS and seats.get(g, 0) > prev.get(g, 0)
        ]
        any_gained = any_gained or bool(gained)
        if gained or test_mode:
            shown = gained if gained else PRIORITY
            lines.append(
                f"▶ {p['label']}\n   " + " / ".join(f"{g} {seats.get(g, 0)}석" for g in shown)
            )
            link = link or p["sac_url"]
        state["seats"][p["goods"]] = seats

    if lines:
        head = (
            "[합창 취소표 2석 이상!] 연석 여부 확인 후 예술의전당에서 예매하세요"
            if any_gained
            else "[합창 감시 테스트] 현재 잔여석"
        )
        body = "\n".join(lines)
        url = link or PERFORMANCES[0]["sac_url"]
        # 하나가 실패해도 다른 하나는 반드시 보낸다. 알람을 먼저 울린다.
        for send in (lambda: alarm_send(head, body, url), lambda: kakao_send(head + "\n" + body, url)):
            try:
                send()
            except Exception as exc:  # noqa: BLE001
                print(f"[알림 실패] {exc}")

    # 좌석 수가 바뀔 때만 파일이 달라지므로, 커밋도 그때만 생긴다.
    save_state(state)


if __name__ == "__main__":
    main()
