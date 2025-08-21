import os
import logging
from celery.signals import worker_ready, task_prerun

log = logging.getLogger(__name__)


def _build_components():
    # 필요한 컴포넌트들을 조립 (idempotent)
    from app.io.quotes_delayed import DelayedQuotesIngestor
    from app.engine.regime import RegimeDetector
    from app.engine.techscore import TechScoreEngine
    from app.engine.mixer import SignalMixer
    from app.engine.risk import RiskEngine
    from app.adapters.paper_ledger import PaperLedger
    from app.io.streams import RedisStreams, StreamConsumer

    comps = {}

    # Redis
    import urllib.parse as u

    rurl = os.getenv("REDIS_URL", "redis://redis:6379/0")
    p = u.urlparse(rurl)
    host = p.hostname or "redis"
    port = int(p.port or 6379)
    db = int((p.path or "/0").lstrip("/") or 0)
    rs = RedisStreams(host=host, port=port, db=db)
    comps["redis_streams"] = rs
    comps["stream_consumer"] = StreamConsumer(rs)

    # Quotes ingestor + 쉬운 warmup
    qi = DelayedQuotesIngestor()
    try:
        qi.warmup_backfill()
        log.info("[autoinit] quotes ingestor warmup_backfill ok")
    except Exception as e:
        log.warning("[autoinit] warmup_backfill skip/err: %s", e)
    comps["quotes_ingestor"] = qi

    # Engines
    # GPT 제안: MIXER_THRESHOLD 사용, 매도는 음수로
    mixer_thr = float(os.getenv("MIXER_THRESHOLD", "0.15"))
    comps["regime_detector"] = RegimeDetector()
    comps["tech_score_engine"] = TechScoreEngine()
    comps["signal_mixer"] = SignalMixer(buy_threshold=mixer_thr, sell_threshold=-mixer_thr)
    # GPT 제안: 명시적 로그로 정합성 확인
    log.info(f"[MixerInit] MIXER={mixer_thr}, buy={mixer_thr}, sell={-mixer_thr}, callsite=autoinit")
    init_cap = float(os.getenv("INITIAL_CAPITAL", "1000000") or 1000000)
    comps["risk_engine"] = RiskEngine(initial_capital=init_cap)
    comps["paper_ledger"] = PaperLedger(initial_cash=init_cap)

    # Slack (있으면)
    try:
        token = os.getenv("SLACK_BOT_TOKEN")
        ch = os.getenv("SLACK_CHANNEL_ID") or os.getenv("SLACK_CHANNEL")
        if token and ch:
            from app.io.slack_bot import SlackBot

            comps["slack_bot"] = SlackBot(token, ch)
    except Exception as e:
        log.warning("[autoinit] slack init skip: %s", e)

    return comps


def _ensure_initialized():
    from app.jobs.scheduler import initialize_components

    try:
        initialize_components(_build_components())
        log.info("[autoinit] initialize_components done")
    except Exception as e:
        log.exception("[autoinit] initialize_components failed: %s", e)


@worker_ready.connect
def _on_ready(sender=None, **kwargs):
    log.info("[autoinit] worker_ready -> ensure init")
    _ensure_initialized()


@task_prerun.connect
def _on_task_prerun(**kwargs):
    # 태스크마다 한 번 더 보장 (가벼운 보호막)
    _ensure_initialized()
