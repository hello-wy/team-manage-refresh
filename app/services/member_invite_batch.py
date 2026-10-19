"""Validate invitation batches and retain each upstream failure reason."""
from app.models import Team
from app.services.account_pool import account_pool_service
from app.utils.seats import normalize_seat_type

COUNTERS = ("unique", "invited", "invalid", "duplicate", "already_exists", "no_seat", "failed", "not_processed")
FATAL_ERRORS = frozenset({"team_not_found", "team_sync_failed", "token_refresh_failed",
                          "team_banned", "account_deactivated", "token_invalidated"})
SEAT_ERRORS = frozenset({"team_full", "seat_limit_exceeded"})


def item_result(email, status, error):
    return {"email": email, "success": False, "status": status, "message": None, "error": error}


def summarize(summary, results):
    invited = summary["invited"]
    failed = sum(summary[key] for key in COUNTERS if key not in {"unique", "invited"})
    if not failed:
        message = f"成功发送 {invited} 个邀请" if invited else "没有需要处理的邮箱"
    else:
        message = f"共处理 {summary['submitted']} 个邮箱：成功 {invited} 个，失败 {failed} 个"
        reasons = list(dict.fromkeys(row["error"] for row in results if row.get("error")))
        message += "；" + "；".join(reasons)
    return {"success": invited > 0, "processed": True, "partial_success": invited > 0 and failed > 0,
            "message": message, "summary": summary, "results": results, "error": None}


class MemberInviteBatchService:
    def __init__(self, teams, db):
        self.teams = teams
        self.db = db

    def _candidates(self, submitted):
        emails, results, seen = [], [], set()
        summary = dict.fromkeys(COUNTERS, 0)
        summary["submitted"] = len(submitted)
        for raw in submitted:
            email = self.teams._normalize_member_email(raw)
            if not email or not self.teams.token_parser.validate_email_format(email):
                summary["invalid"] += 1
                results.append(item_result(str(raw or "").strip(), "invalid_email", "邮箱格式不正确"))
                continue
            if email in seen:
                summary["duplicate"] += 1
                results.append(item_result(email, "duplicate", "重复邮箱，已跳过"))
                continue
            seen.add(email)
            emails.append(email)
        summary["unique"] = len(emails)
        return emails, summary, results

    async def invite(self, team_id, emails, *, seat_type=None, allow_reinvite=False):
        seat = {"standard": "default", "premium": "premium"}.get(normalize_seat_type(seat_type or "default"))
        if seat is None:
            return self.teams._admin_error("invalid_seat_type", "不支持的席位类型")
        candidates, summary, results = self._candidates(list(emails or []))
        if not candidates:
            return summarize(summary, results)
        sync = await self.teams.sync_team_info(team_id, self.db)
        if not sync.get("success"):
            return self.teams._admin_error(
                sync.get("error_code") or "team_sync_failed",
                sync.get("error") or "拉取 Team 最新成员状态失败，请稍后重试",
                processed=False, partial_success=False, summary=summary, results=results,
            )
        team = await self.db.get(Team, team_id)
        if team is None:
            return self.teams._admin_error("team_not_found", "Team 不存在", processed=False,
                                           summary=summary, results=results)
        existing = {self.teams._normalize_member_email(email) for email in sync.get("member_emails", [])}
        required = sum(email not in existing for email in candidates)
        if required:
            balance = await self.teams.get_team_seat_balance(team, self.db)
            error = self.teams._seat_balance_error(balance, seat, required)
            if error:
                return {**error, "processed": False, "summary": summary, "results": results}
        queued = []
        for email in candidates:
            if email in existing:
                summary["already_exists"] += 1
                results.append(item_result(email, "already_exists", "该邮箱已在当前 Team 中"))
            elif await self._recently_joined(team_id, email):
                summary["failed"] += 1
                results.append(item_result(email, "recently_joined", "该账号 7 天内加入过当前 Team，不能重复邀请"))
            else:
                queued.append(email)
        outcomes = await self._send(queued, team_id, seat_type=seat, allow_reinvite=allow_reinvite)
        for row, counter in outcomes:
            summary[counter] += 1
            results.append(row)
        return summarize(summary, results)

    async def _recently_joined(self, team_id, email):
        entry = await account_pool_service.get_by_email(self.db, email)
        return bool(entry and await account_pool_service.is_recently_joined(team_id, entry.id, self.db))

    async def _send(self, emails, team_id, *, seat_type, allow_reinvite):
        stopped, outcomes = False, []
        for email in emails:
            if stopped:
                row = item_result(email, "not_processed", "处理过程中 Team 状态异常，后续邮箱未继续执行")
                outcomes.append((row, "not_processed"))
                continue
            options = {"seat_type": seat_type}
            if allow_reinvite:
                options["allow_reinvite"] = True
            result = await self.teams.add_team_member(team_id, email, self.db, **options)
            outcomes.append(self._outcome(email, result))
            stopped = result.get("error_code") in FATAL_ERRORS
        return outcomes

    @staticmethod
    def _outcome(email, result):
        status = result.get("status") or ("invited" if result.get("success") else "failed")
        if result.get("error_code") in SEAT_ERRORS:
            status = "no_seat"
        counter = status if status in {"already_exists", "no_seat"} else "failed"
        if status == "invited" and result.get("success"):
            counter = "invited"
        return {"email": result.get("email") or email, "success": bool(result.get("success")),
                "status": status, "message": result.get("message"), "error": result.get("error"),
                "error_code": result.get("error_code")}, counter
