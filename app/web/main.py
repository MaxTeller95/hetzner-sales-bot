"""Admin dashboard.  Usage: uvicorn app.web.main:app"""
from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.bot import texts as T
from app.config import get_settings
from app.db.models import (
    AbuseReport,
    AbuseStatus,
    BillingEvent,
    IPAddress,
    IPStatus,
    Job,
    JobStatus,
    Location,
    OSImage,
    Plan,
    Provider,
    Server,
    ServerStatus,
    TopUpRequest,
    TopUpStatus,
    Transaction,
    User,
)
from app.providers.registry import PROVIDER_CLASSES, close_all
from app.services import abuse as abuse_svc
from app.services import catalog, jobs, stats, topups
from app.services import pool as pool_svc
from app.services import servers as server_svc
from app.services import wallet as wallet_svc
from app.services.errors import UserError
from app.services.pricing import PricingNotConfigured, quote_plan
from app.services.runtime_settings import DEFS, load_settings, save_settings
from app.utils import decrypt, encrypt, parse_amount, utcnow
from app.web.deps import (
    LoginRequired,
    back,
    close_notifier,
    flash,
    get_session,
    notifier,
    render,
    require_admin,
    verify_csrf,
)

cfg = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg.check_production()
    yield
    await close_notifier()
    await close_all()


app = FastAPI(title="Server Shop Admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=cfg.secret_key,
    session_cookie="admin_session",
    https_only=cfg.session_https_only,
    same_site="lax",
    max_age=60 * 60 * 12,
)

Admin = Depends(require_admin)
DB = Depends(get_session)
CSRF = Depends(verify_csrf)


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


async def _notify(chat_id: int, text: str) -> None:
    n = notifier()
    if n:
        await n.send(chat_id, text)


# ---- auth ---------------------------------------------------------------------------
_failed_logins: dict[str, list[float]] = {}


@app.get("/login")
async def login_page(request: Request):
    return render(request, "login.html", configured=bool(cfg.admin_password))


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failed_logins.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 5:
        flash(request, "Too many failed attempts. Try again in 10 minutes.", "error")
        return RedirectResponse("/login", status_code=303)
    ok = (
        bool(cfg.admin_password)
        and secrets.compare_digest(username, cfg.admin_username)
        and secrets.compare_digest(password, cfg.admin_password)
    )
    if not ok:
        _failed_logins[ip] = recent + [time.time()]
        flash(request, "Invalid username or password.", "error")
        return RedirectResponse("/login", status_code=303)
    _failed_logins.pop(ip, None)
    request.session.clear()
    request.session["admin"] = username
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- overview -----------------------------------------------------------------------
@app.get("/")
async def dashboard(request: Request, admin=Admin, session: AsyncSession = DB):
    st = await stats.overview(session)
    recent_servers = (await session.scalars(select(Server).where(Server.user_id.is_not(None)).order_by(Server.id.desc()).limit(8))).all()
    return render(request, "dashboard.html", st=st, recent_servers=recent_servers)


@app.get("/api/stats")
async def api_stats(admin=Admin, session: AsyncSession = DB):
    return JSONResponse(await stats.overview(session))


# ---- users -------------------------------------------------------------------------
@app.get("/users")
async def users_page(request: Request, q: str = "", admin=Admin, session: AsyncSession = DB):
    query = select(User).order_by(User.id.desc()).limit(200)
    if q:
        like = f"%{q.lstrip('@')}%"
        conds = [User.username.ilike(like), User.first_name.ilike(like)]
        if q.isdigit():
            conds.append(User.telegram_id == int(q))
        query = query.where(or_(*conds))
    users = (await session.scalars(query)).all()
    counts = dict(
        (
            await session.execute(
                select(Server.user_id, func.count(Server.id))
                .where(Server.status.in_(ServerStatus.BILLABLE))
                .group_by(Server.user_id)
            )
        ).all()
    )
    return render(request, "users.html", users=users, counts=counts, q=q)


@app.get("/users/{user_id}")
async def user_page(request: Request, user_id: int, admin=Admin, session: AsyncSession = DB):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    servers = (await session.scalars(select(Server).where(Server.user_id == user_id).order_by(Server.id.desc()))).all()
    ips = (await session.scalars(select(IPAddress).where(IPAddress.user_id == user_id).order_by(IPAddress.id.desc()))).all()
    txs = (await session.scalars(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.id.desc()).limit(100))).all()
    reqs = (await session.scalars(select(TopUpRequest).where(TopUpRequest.user_id == user_id).order_by(TopUpRequest.id.desc()).limit(20))).all()
    burn = await wallet_svc.hourly_burn(session, user_id)
    return render(request, "user.html", user=user, servers=servers, ips=ips, txs=txs, reqs=reqs, burn=burn)


@app.post("/users/{user_id}/adjust", dependencies=[CSRF])
async def user_adjust(request: Request, user_id: int, amount: str = Form(...), note: str = Form(""), notify: str = Form(""), admin=Admin, session: AsyncSession = DB):
    value = parse_amount(amount)
    if not value:
        flash(request, "Enter a non-zero amount.", "error")
        return back(request)
    if amount.strip().startswith("-"):
        value = -value
    user = await session.get(User, user_id)
    resumed = await topups.admin_adjust(session, user_id, value, note or f"Admin adjustment by {admin}")
    await session.commit()
    if notify:
        wallet = await wallet_svc.get_wallet(session, user_id)
        if value > 0:
            await _notify(user.telegram_id, T.topup_approved(value, wallet.balance))
        else:
            await _notify(user.telegram_id, f"➖ {abs(value):,} ریال از کیف پول شما کسر شد.\n{note}")
        if resumed:
            await _notify(user.telegram_id, T.RESUMED)
    flash(request, f"Wallet adjusted by {value:,} IRR.")
    return back(request)


@app.post("/users/{user_id}/ban", dependencies=[CSRF])
async def user_ban(request: Request, user_id: int, admin=Admin, session: AsyncSession = DB):
    user = await session.get(User, user_id)
    user.is_banned = not user.is_banned
    await session.commit()
    flash(request, f"User {'banned' if user.is_banned else 'unbanned'}.")
    return back(request)


@app.post("/users/{user_id}/note", dependencies=[CSRF])
async def user_note(request: Request, user_id: int, note: str = Form(""), admin=Admin, session: AsyncSession = DB):
    user = await session.get(User, user_id)
    user.admin_note = note
    await session.commit()
    flash(request, "Note saved.")
    return back(request)


@app.post("/users/{user_id}/message", dependencies=[CSRF])
async def user_message(request: Request, user_id: int, text: str = Form(...), admin=Admin, session: AsyncSession = DB):
    user = await session.get(User, user_id)
    n = notifier()
    sent = n and await n.send(user.telegram_id, text)
    flash(request, "Message sent." if sent else "Message could not be delivered.", "ok" if sent else "error")
    return back(request)


# ---- servers ----------------------------------------------------------------------------
@app.get("/servers")
async def servers_page(request: Request, status: str = "live", q: str = "", admin=Admin, session: AsyncSession = DB):
    query = select(Server).order_by(Server.id.desc()).limit(300)
    if status == "live":
        query = query.where(Server.status.in_(ServerStatus.VISIBLE + (ServerStatus.POOLED,)))
    elif status == "locked":
        query = query.where(Server.locked.is_(True))
    elif status != "all":
        query = query.where(Server.status == status)
    if q:
        query = query.where(or_(Server.name.ilike(f"%{q}%"), Server.ipv4.ilike(f"%{q}%")))
    servers = (await session.scalars(query)).all()
    return render(request, "servers.html", servers=servers, status=status, q=q)


@app.get("/servers/{server_id}")
async def server_page(request: Request, server_id: int, admin=Admin, session: AsyncSession = DB):
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404)
    events = (await session.scalars(select(BillingEvent).where(BillingEvent.server_id == server_id).order_by(BillingEvent.id.desc()).limit(100))).all()
    server_jobs = (await session.scalars(select(Job).where(Job.server_id == server_id).order_by(Job.id.desc()).limit(50))).all()
    reports = (await session.scalars(select(AbuseReport).where(AbuseReport.server_id == server_id).order_by(AbuseReport.id.desc()))).all()
    revenue = -int(await session.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.server_id == server_id)) or 0)
    return render(
        request, "server.html", server=server, events=events, jobs=server_jobs, reports=reports,
        password=decrypt(server.root_password_enc), revenue=revenue,
    )


@app.post("/servers/{server_id}/action", dependencies=[CSRF])
async def server_action(request: Request, server_id: int, action: str = Form(...), reason: str = Form(""), admin=Admin, session: AsyncSession = DB):
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404)
    user_msg = None
    try:
        if action in ("reboot", "shutdown", "poweron", "poweroff"):
            await server_svc.ensure_idle(session, server)
            await jobs.enqueue(session, "power", {"action": action, "silent": True}, server_id=server.id)
        elif action == "delete":
            await server_svc.request_delete(session, server, by_admin=True)
        elif action == "lock":
            await abuse_svc.lock_server(session, server, reason=reason or "Locked by admin", source="admin", subject=reason or "Locked by admin")
            user_msg = T.abuse_locked(server)
        elif action == "unlock":
            await abuse_svc.unlock_server(session, server)
            user_msg = T.abuse_unlocked(server)
        elif action == "deliver":
            await server_svc.deliver(session, server)
        else:
            raise UserError("unknown action")
    except UserError as e:
        flash(request, str(e), "error")
        return back(request)
    await session.commit()
    if user_msg and server.user:
        await _notify(server.user.telegram_id, user_msg)
    flash(request, f"{action} queued for {server.name}.")
    return back(request)


# ---- top-ups -----------------------------------------------------------------------------
@app.get("/topups")
async def topups_page(request: Request, status: str = "pending", admin=Admin, session: AsyncSession = DB):
    query = select(TopUpRequest).order_by(TopUpRequest.id.desc()).limit(200)
    if status != "all":
        query = query.where(TopUpRequest.status == status)
    return render(request, "topups.html", reqs=(await session.scalars(query)).all(), status=status)


@app.get("/topups/{req_id}/receipt")
async def topup_receipt(req_id: int, admin=Admin, session: AsyncSession = DB):
    req = await session.get(TopUpRequest, req_id)
    n = notifier()
    if req is None or not req.receipt_file_id or n is None:
        raise HTTPException(404)
    file = await n.bot.get_file(req.receipt_file_id)
    data = await n.bot.download_file(file.file_path)
    media = "application/pdf" if (file.file_path or "").endswith(".pdf") else "image/jpeg"
    return Response(data.read(), media_type=media, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/topups/{req_id}/approve", dependencies=[CSRF])
async def topup_approve(request: Request, req_id: int, amount: str = Form(""), admin=Admin, session: AsyncSession = DB):
    result = await topups.approve(session, req_id, f"web:{admin}", parse_amount(amount) if amount else None)
    if result is None:
        flash(request, "Request is no longer pending.", "error")
        return back(request)
    req, resumed = result
    await session.commit()
    wallet = await wallet_svc.get_wallet(session, req.user_id)
    await _notify(req.user.telegram_id, T.topup_approved(req.amount, wallet.balance))
    if resumed:
        await _notify(req.user.telegram_id, T.RESUMED)
    flash(request, f"Top-up #{req.id} approved ({req.amount:,} IRR).")
    return back(request)


@app.post("/topups/{req_id}/reject", dependencies=[CSRF])
async def topup_reject(request: Request, req_id: int, note: str = Form(""), admin=Admin, session: AsyncSession = DB):
    req = await topups.reject(session, req_id, f"web:{admin}", note or None)
    if req is None:
        flash(request, "Request is no longer pending.", "error")
        return back(request)
    await session.commit()
    await _notify(req.user.telegram_id, T.topup_rejected(req.id, note or None))
    flash(request, f"Top-up #{req.id} rejected.")
    return back(request)


# ---- catalog: plans, locations, images ------------------------------------------------------
@app.get("/plans")
async def plans_page(request: Request, location_id: int = 0, admin=Admin, session: AsyncSession = DB):
    s = await load_settings(session)
    locations = (await session.scalars(select(Location).order_by(Location.provider_id, Location.sort_order))).all()
    query = select(Plan).order_by(Plan.location_id, Plan.sort_order, Plan.base_hourly)
    if location_id:
        query = query.where(Plan.location_id == location_id)
    rows = []
    for plan in (await session.scalars(query)).all():
        try:
            quote = quote_plan(plan, plan.location, s)
        except PricingNotConfigured:
            quote = None
        rows.append((plan, quote))
    return render(request, "plans.html", rows=rows, locations=locations, location_id=location_id, pricing_ok=bool(s.exchange_rate_eur))


def _decimal_or_none(value: str) -> Decimal | None:
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        raise UserError(f"invalid number: {value}") from None


@app.post("/plans/{plan_id}", dependencies=[CSRF])
async def plan_update(
    request: Request, plan_id: int,
    is_active: str = Form(""), override_hourly: str = Form(""), pool_target: int = Form(0), sort_order: int = Form(0),
    admin=Admin, session: AsyncSession = DB,
):
    plan = await session.get(Plan, plan_id)
    try:
        plan.override_hourly = _decimal_or_none(override_hourly)
    except UserError as e:
        flash(request, str(e), "error")
        return back(request)
    plan.is_active = bool(is_active)
    plan.pool_target = max(0, pool_target)
    plan.sort_order = sort_order
    await session.commit()
    flash(request, f"Plan {plan.name} @ {plan.location.code} saved.")
    return back(request)


@app.post("/plans-bulk", dependencies=[CSRF])
async def plans_bulk(request: Request, location_id: int = Form(0), active: str = Form(...), admin=Admin, session: AsyncSession = DB):
    query = select(Plan)
    if location_id:
        query = query.where(Plan.location_id == location_id)
    for plan in (await session.scalars(query)).all():
        plan.is_active = active == "1"
    await session.commit()
    flash(request, "Plans updated.")
    return back(request)


@app.get("/catalog")
async def catalog_page(request: Request, admin=Admin, session: AsyncSession = DB):
    locations = (await session.scalars(select(Location).order_by(Location.provider_id, Location.sort_order))).all()
    images = (await session.scalars(select(OSImage).order_by(OSImage.provider_id, OSImage.sort_order))).all()
    return render(request, "catalog.html", locations=locations, images=images)


@app.post("/locations/{loc_id}", dependencies=[CSRF])
async def location_update(request: Request, loc_id: int, name: str = Form(...), is_active: str = Form(""), sort_order: int = Form(0), admin=Admin, session: AsyncSession = DB):
    loc = await session.get(Location, loc_id)
    loc.name, loc.is_active, loc.sort_order = name, bool(is_active), sort_order
    await session.commit()
    flash(request, f"Location {loc.code} saved.")
    return back(request)


@app.post("/images/{image_id}", dependencies=[CSRF])
async def image_update(request: Request, image_id: int, name: str = Form(...), is_active: str = Form(""), sort_order: int = Form(0), admin=Admin, session: AsyncSession = DB):
    img = await session.get(OSImage, image_id)
    img.name, img.is_active, img.sort_order = name, bool(is_active), sort_order
    await session.commit()
    flash(request, f"Image {img.code} saved.")
    return back(request)


# ---- providers ----------------------------------------------------------------------------
@app.get("/providers")
async def providers_page(request: Request, admin=Admin, session: AsyncSession = DB):
    providers = (await session.scalars(select(Provider).order_by(Provider.sort_order, Provider.id))).all()
    return render(request, "providers.html", providers=providers, kinds=sorted(PROVIDER_CLASSES))


@app.post("/providers", dependencies=[CSRF])
async def provider_add(request: Request, code: str = Form(...), kind: str = Form(...), name: str = Form(...), api_token: str = Form(""), admin=Admin, session: AsyncSession = DB):
    if kind not in PROVIDER_CLASSES:
        flash(request, "Unknown provider kind.", "error")
        return back(request)
    if await session.scalar(select(Provider.id).where(Provider.code == code)):
        flash(request, "A provider with this code already exists.", "error")
        return back(request)
    session.add(Provider(code=code.strip().lower(), kind=kind, name=name, api_token_enc=encrypt(api_token.strip()), options={}))
    await session.commit()
    flash(request, f"Provider {code} added. Press Sync to import its catalog.")
    return back(request)


@app.post("/providers/{provider_id}", dependencies=[CSRF])
async def provider_update(request: Request, provider_id: int, name: str = Form(...), api_token: str = Form(""), is_active: str = Form(""), sort_order: int = Form(0), admin=Admin, session: AsyncSession = DB):
    row = await session.get(Provider, provider_id)
    row.name, row.is_active, row.sort_order = name, bool(is_active), sort_order
    if api_token.strip():
        row.api_token_enc = encrypt(api_token.strip())
    await session.commit()
    flash(request, f"Provider {row.code} saved.")
    return back(request)


@app.post("/providers/{provider_id}/sync", dependencies=[CSRF])
async def provider_sync(request: Request, provider_id: int, admin=Admin, session: AsyncSession = DB):
    row = await session.get(Provider, provider_id)
    try:
        counts = await catalog.sync_provider(session, row)
    except Exception as e:  # noqa: BLE001 - show any provider error to the admin
        await session.rollback()
        flash(request, f"Sync failed: {e}", "error")
        return back(request)
    await session.commit()
    flash(request, f"Synced {row.code}: {counts}")
    return back(request)


# ---- pool --------------------------------------------------------------------------------
@app.get("/pool")
async def pool_page(request: Request, admin=Admin, session: AsyncSession = DB):
    pooled = (await session.scalars(select(Server).where(Server.user_id.is_(None), Server.status.in_(pool_svc.POOL_FILLING + (ServerStatus.FAILED,))).order_by(Server.id.desc()).limit(200))).all()
    plans = (await session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.location_id, Plan.base_hourly))).all()
    targets = [p for p in plans if p.pool_target > 0]
    return render(request, "pool.html", pooled=pooled, plans=plans, targets=targets)


@app.post("/pool/create", dependencies=[CSRF])
async def pool_create(request: Request, plan_id: int = Form(...), count: int = Form(1), admin=Admin, session: AsyncSession = DB):
    plan = await session.get(Plan, plan_id)
    created = 0
    for _ in range(max(1, min(count, 20))):
        if await pool_svc.create_pool_server(session, plan):
            created += 1
    await session.commit()
    flash(request, f"Queued {created} pool server(s) for {plan.name} @ {plan.location.code}." if created else "No suitable OS image for this plan.", "ok" if created else "error")
    return back(request)


# ---- abuse --------------------------------------------------------------------------------
@app.get("/abuse")
async def abuse_page(request: Request, status: str = "open", admin=Admin, session: AsyncSession = DB):
    query = select(AbuseReport).order_by(AbuseReport.id.desc()).limit(200)
    if status != "all":
        query = query.where(AbuseReport.status == status)
    return render(request, "abuse.html", reports=(await session.scalars(query)).all(), status=status)


@app.post("/abuse/{report_id}", dependencies=[CSRF])
async def abuse_resolve(request: Request, report_id: int, action: str = Form(...), note: str = Form(""), admin=Admin, session: AsyncSession = DB):
    report = await session.get(AbuseReport, report_id)
    if report is None:
        raise HTTPException(404)
    unlock = action == "unlock"
    await abuse_svc.resolve(session, report, unlock=unlock, note=note or None, dismiss=action == "dismiss")
    await session.commit()
    if unlock and report.server and report.server.user:
        await _notify(report.server.user.telegram_id, T.abuse_unlocked(report.server))
    flash(request, f"Report #{report.id} {report.status}.")
    return back(request)


# ---- settings -----------------------------------------------------------------------------
@app.get("/settings")
async def settings_page(request: Request, admin=Admin, session: AsyncSession = DB):
    values = (await load_settings(session, fresh=True)).as_dict()
    groups: dict[str, list] = {}
    for d in DEFS:
        groups.setdefault(d.group, []).append(d)
    return render(request, "settings.html", groups=groups, values=values)


@app.post("/settings", dependencies=[CSRF])
async def settings_save(request: Request, admin=Admin, session: AsyncSession = DB):
    form = await request.form()
    values = {}
    for d in DEFS:
        if d.type is bool:
            values[d.key] = "true" if form.get(d.key) else "false"
        elif d.key in form:
            values[d.key] = str(form[d.key]).strip() if not d.multiline else str(form[d.key]).replace("\r\n", "\n").strip()
    await save_settings(session, values)
    await session.commit()
    flash(request, "Settings saved.")
    return back(request)


# ---- jobs & transactions ----------------------------------------------------------------------
@app.get("/jobs")
async def jobs_page(request: Request, status: str = "all", admin=Admin, session: AsyncSession = DB):
    query = select(Job).order_by(Job.id.desc()).limit(300)
    if status != "all":
        query = query.where(Job.status == status)
    return render(request, "jobs.html", jobs=(await session.scalars(query)).all(), status=status)


@app.post("/jobs/{job_id}/retry", dependencies=[CSRF])
async def job_retry(request: Request, job_id: int, admin=Admin, session: AsyncSession = DB):
    job = await session.get(Job, job_id)
    if job and job.status == JobStatus.FAILED:
        job.status, job.run_at, job.attempts, job.finished_at = JobStatus.PENDING, utcnow(), 0, None
        await session.commit()
        flash(request, f"Job #{job_id} re-queued.")
    return back(request)


@app.get("/transactions")
async def transactions_page(request: Request, kind: str = "all", admin=Admin, session: AsyncSession = DB):
    query = select(Transaction).order_by(Transaction.id.desc()).limit(300)
    if kind != "all":
        query = query.where(Transaction.kind == kind)
    txs = (await session.scalars(query)).all()
    users = {u.id: u for u in (await session.scalars(select(User).where(User.id.in_({t.user_id for t in txs})))).all()} if txs else {}
    return render(request, "transactions.html", txs=txs, users=users, kind=kind)


@app.get("/ips")
async def ips_page(request: Request, admin=Admin, session: AsyncSession = DB):
    ips = (await session.scalars(select(IPAddress).where(IPAddress.status.in_(IPStatus.VISIBLE)).order_by(IPAddress.id.desc()))).all()
    return render(request, "ips.html", ips=ips)


@app.get("/healthz")
async def healthz(session: AsyncSession = DB):
    await session.execute(select(1))
    return {"ok": True}
