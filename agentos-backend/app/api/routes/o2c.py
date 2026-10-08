"""O2C_OHC — HTTP surface only; business logic lives in ``app.services.o2c``."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator

from app.agents.o2c_ohc.billing_constants import (
    INVOICE_ADMIN_BASE_STAFFING_ONLY,
    INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY,
)
from app.api.deps import get_current_user
from app.db.models import User
from app.schemas.o2c import (
    ContractRateLineBulkPatchBody,
    ContractReviewHeaderPatchBody,
    ContractReviewStatusBody,
)
from app.services.o2c.attendance_map import load_ohc_attendance_map_api
from app.services.o2c.catalog import (
    list_active_service_sites_for_picker,
    list_available_rate_lines_for_mis_run,
    list_billing_clients,
)
from app.services.o2c.contract_review import (
    contract_review_failures,
    contract_review_get,
    contract_review_list,
    contract_review_rate_lines_patch,
    contract_review_set_status,
    parse_contract_review_statuses_query,
)
from app.services.o2c.contract_review_health import (
    contract_review_client_tree,
    contract_review_ops_summary,
    contract_review_patch_header,
    contract_review_siblings,
)
from app.services.o2c.ingest_api import run_contract_folder_ingest
from app.services.o2c.mis_summary_api import (
    create_new_mis_summary_row_api,
    delete_mis_summary_row_api,
    insert_mis_summary_row_api,
    restore_mis_summary_row_api,
    set_mis_status_api,
)
from app.services.o2c.mis_contract_lines import (
    copy_contract_rate_lines_to_mis_run,
    list_alternate_contract_terms_for_mis_run,
)
from app.services.o2c.mis_workflow import (
    approve_mis_with_xlsx_export,
    generate_mis_draft_api,
    get_mis_run_or_404,
    list_mis_runs_with_counts,
    rerun_mis_for_existing_run,
    save_and_approve_mis_with_xlsx_export,
    save_mis_with_rerun,
)
from app.services.o2c.recon_workflow import (
    list_open_attendance_recon,
    resolve_attendance_recon_and_rerun,
    resolve_attendance_recon_simple,
)
from app.services.o2c.reingest import reingest_failed_contract_parsing
from app.services.o2c.service_site import clone_site_rate_lines_for_api, create_service_site_with_recon
from app.services.o2c.site_alias_flow import site_alias_upsert_with_optional_mis_rerun

router = APIRouter()


class IngestContractsBody(BaseModel):
    contracts_root: str | None = None
    max_files: int | None = Field(None, ge=1, le=500)
    ignore_mtime_watermark: bool = Field(
        False,
        description=(
            "If true, scan all PDFs under the root regardless of file mtime vs ingestion watermark. "
            "Use when files were added recently but copy/unzip kept an old modified time. "
            "Duplicates are still skipped by sha256."
        ),
    )


class AttendanceBody(BaseModel):
    xlsx_path: str | None = None
    sheet_name: str | None = None
    include_rows: bool = False


class AttendanceReconQuery(BaseModel):
    limit: int = Field(200, ge=1, le=1000)


class ResolveAttendanceReconBody(BaseModel):
    recon_id: UUID
    service_site_id: UUID
    alias_code: str = Field(..., min_length=1, max_length=500)
    resolution_notes: str | None = Field(None, max_length=2000)


class ResolveAttendanceReconAndRerunBody(BaseModel):
    recon_id: UUID
    service_site_id: UUID
    alias_code: str | None = Field(None, min_length=1, max_length=500)
    alias_display: str | None = Field(None, max_length=500)
    source_system: str = Field("manual", min_length=1, max_length=100)
    resolution_notes: str | None = Field(None, max_length=2000)
    auto_run_previous_month_mis: bool = True
    use_recon_period: bool = False
    allow_alias_reassign: bool = False
    allow_expired_contract_terms: bool = False


class CreateServiceSiteBody(BaseModel):
    site_key: str = Field(..., min_length=1, max_length=500)
    display_name: str | None = Field(None, max_length=500)
    billing_client_id: UUID
    recon_id: UUID = Field(
        ...,
        description="Open o2c_attendance_site_recon row being cleared by this create (required).",
    )
    alias_code: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Must match the recon row's client_site_key (attendance export label).",
    )
    auto_run_previous_month_mis: bool = True
    clone_rate_lines_from_site_id: UUID | None = None
    replace_existing_site_rate_lines: bool = False
    require_same_terms_for_period: bool = False
    clone_terms_period_start: str | None = Field(None, min_length=10, max_length=10)
    clone_terms_period_end: str | None = Field(None, min_length=10, max_length=10)
    allow_expired_contract_terms: bool = False


class CloneSiteRateLinesBody(BaseModel):
    source_service_site_id: UUID
    target_service_site_id: UUID
    replace_existing: bool = False
    require_same_terms_for_period: bool = False
    period_start: str | None = Field(None, min_length=10, max_length=10)
    period_end: str | None = Field(None, min_length=10, max_length=10)


class ListMisRunsBody(BaseModel):
    status: str | None = Field("pending_human", max_length=50)
    limit: int = Field(200, ge=1, le=1000)
    period_scope: str = Field(
        "prior_month",
        description=(
            "prior_month (default): billing_period_start in the previous calendar month (Asia/Kolkata). "
            "current_month: same for the current IST calendar month. "
            "all: no period filter."
        ),
    )

    @field_validator("period_scope")
    @classmethod
    def _validate_period_scope(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if s not in ("prior_month", "current_month", "all"):
            raise ValueError("period_scope must be 'prior_month', 'current_month', or 'all'")
        return s


class GetMisRunBody(BaseModel):
    mis_run_id: UUID


class RerunMisBody(BaseModel):
    mis_run_id: UUID
    allow_expired_contract_terms: bool = False


class GenerateMisDraftBody(BaseModel):
    attendance_xlsx: str | None = Field(
        None,
        max_length=2000,
        description=(
            "Optional local OHC workbook .xlsx. If omitted, uses O2C_ATTENDANCE_XLSX_PATH / "
            "O2C_OHC_ATTENDANCE_XLSX_PATH or exports O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID."
        ),
    )
    client_site_key: str = Field(..., min_length=1, max_length=500)
    period_start: str = Field(..., min_length=10, max_length=10)
    period_end: str = Field(..., min_length=10, max_length=10)
    out_dir: str = Field(..., min_length=1, max_length=2000)
    template_path: str = Field(..., min_length=1, max_length=2000)
    allow_expired_contract_terms: bool | None = Field(
        None,
        description=(
            "If true, use relaxed contract_terms_version pick when calendar window misses the period "
            "(same as MIS recon rerun). If omitted, uses settings.o2c_mis_allow_expired_contract_terms."
        ),
    )


class DeleteMisSummaryRowBody(BaseModel):
    mis_summary_row_id: UUID


class RestoreMisSummaryRowBody(BaseModel):
    mis_summary_row_id: UUID


class InsertMisSummaryRowBody(BaseModel):
    mis_run_id: UUID
    contract_rate_line_id: UUID


class CreateNewMisSummaryRowBody(BaseModel):
    mis_run_id: UUID
    description: str = Field(..., min_length=1, max_length=500)
    role_code: str = Field(..., min_length=1, max_length=200)
    rate_amount: Decimal | None = None
    contracted_quantity: Decimal | None = None
    """Headcount cap (e.g. roster lines). Not used for visit counts on ``per_visit``."""

    billing_model: str = Field("fixed_monthly", max_length=50)
    """When set, creates ``OHC_ADMIN_INVOICE_PCT`` with ``billing_rules.invoice_admin_pct`` (rate_amount ignored)."""

    visit_per_month: Decimal | None = None
    """Optional frozen visit count for ``per_visit``. Prefer ``visits_per_week`` when cadence is weekly."""

    visits_per_week: Decimal | None = None
    """Weekly cadence for ``per_visit``; this MIS period bills cadence × 4 or 5 weeks (ceil days/7)."""

    invoice_admin_pct: Decimal | None = None
    """``staffing_only`` (default) or ``staffing_plus_fixed_monthly``; only with ``invoice_admin_pct``."""

    invoice_admin_base: str | None = Field(None, max_length=64)

    @staticmethod
    def _validate_invoice_pct(v: Decimal) -> Decimal:
        x = Decimal(v)
        if x <= 0 or x > 100:
            raise ValueError("invoice_admin_pct must be between 0 and 100 exclusive of 0")
        return x

    @model_validator(mode="after")
    def _rate_or_invoice_admin(self) -> CreateNewMisSummaryRowBody:
        bab = (self.invoice_admin_base or "").strip()
        if bab and self.invoice_admin_pct is None:
            raise ValueError("invoice_admin_base is only allowed when invoice_admin_pct is set")
        if bab and bab not in (
            INVOICE_ADMIN_BASE_STAFFING_ONLY,
            INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY,
        ):
            raise ValueError(
                "invoice_admin_base must be staffing_only or staffing_plus_fixed_monthly"
            )
        self.invoice_admin_base = bab or None
        if self.invoice_admin_pct is not None:
            self.invoice_admin_pct = self._validate_invoice_pct(self.invoice_admin_pct)
            return self
        if self.rate_amount is None:
            raise ValueError("rate_amount is required unless invoice_admin_pct is set")
        week = self.visits_per_week is not None and self.visits_per_week > 0
        month = self.visit_per_month is not None and self.visit_per_month > 0
        if week and month:
            raise ValueError("per_visit: set visits_per_week or visit_per_month, not both")
        return self


class SetMisStatusBody(BaseModel):
    mis_run_id: UUID
    status: str = Field(..., min_length=1, max_length=50)  # pending_human|approved|rejected
    rejection_notes: str | None = Field(None, max_length=2000)


class SiteAliasUpsertBody(BaseModel):
    service_site_id: UUID
    alias_code: str = Field(..., min_length=1, max_length=500)
    alias_display: str | None = Field(None, max_length=500)
    source_system: str = Field("manual", min_length=1, max_length=100)
    auto_run_previous_month_mis: bool = True
    allow_expired_contract_terms: bool = False


class SaveMisBody(BaseModel):
    mis_run_id: UUID
    row_edits: list[dict[str, Any]] = Field(default_factory=list)
    allow_expired_contract_terms: bool = False


class ApproveMisBody(BaseModel):
    mis_run_id: UUID


class SaveAndApproveMisBody(BaseModel):
    mis_run_id: UUID
    row_edits: list[dict[str, Any]] = Field(default_factory=list)
    allow_expired_contract_terms: bool = False


class AvailableRateLinesBody(BaseModel):
    mis_run_id: UUID


class CopyContractRateLinesBody(BaseModel):
    mis_run_id: UUID
    source_contract_terms_version_id: UUID


class AlternateContractTermsBody(BaseModel):
    mis_run_id: UUID


@router.post("/ingest-contracts")
async def o2c_ingest_contracts(
    body: IngestContractsBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await run_contract_folder_ingest(
        contracts_root=body.contracts_root,
        max_files=body.max_files,
        ignore_mtime_watermark=body.ignore_mtime_watermark,
    )


@router.post("/ohc-attendance-map")
async def o2c_ohc_attendance_map(
    body: AttendanceBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await load_ohc_attendance_map_api(
        xlsx_path=body.xlsx_path,
        sheet_name=body.sheet_name,
        include_rows=body.include_rows,
    )


@router.post("/attendance-recon/open")
async def o2c_list_open_attendance_recon(
    body: AttendanceReconQuery,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await list_open_attendance_recon(limit=body.limit)


@router.post("/attendance-recon/resolve")
async def o2c_resolve_attendance_recon(
    body: ResolveAttendanceReconBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await resolve_attendance_recon_simple(
        recon_id=body.recon_id,
        service_site_id=body.service_site_id,
        alias_code=body.alias_code,
        resolution_notes=body.resolution_notes,
        resolved_by=user.email,
    )


@router.get("/contracts/review/summary")
async def o2c_contract_review_summary_get(
    _user: Annotated[User, Depends(get_current_user)],
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    _ = _user
    return await contract_review_ops_summary(period_start=period_start, period_end=period_end)


@router.get("/contracts/review/clients/{billing_client_id}/tree")
async def o2c_contract_review_client_tree_get(
    billing_client_id: UUID,
    _user: Annotated[User, Depends(get_current_user)],
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    _ = _user
    return await contract_review_client_tree(
        billing_client_id=str(billing_client_id),
        period_start=period_start,
        period_end=period_end,
    )


@router.get("/contracts/review")
async def o2c_contract_review_list_get(
    _user: Annotated[User, Depends(get_current_user)],
    statuses: str = "draft,pending",
    search: str = "",
    page: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    _ = _user
    return await contract_review_list(
        statuses=parse_contract_review_statuses_query(statuses),
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/contracts/review/failures")
async def o2c_contract_review_failures_get(
    _user: Annotated[User, Depends(get_current_user)],
    limit: int = 200,
    open_only: bool = True,
) -> dict[str, Any]:
    _ = _user
    return await contract_review_failures(limit=limit, open_only=open_only)


@router.get("/contracts/review/contract_term/{contract_terms_version_id}")
async def o2c_contract_review_get_by_id(
    contract_terms_version_id: UUID,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await contract_review_get(contract_terms_version_id=str(contract_terms_version_id))


@router.get("/contracts/review/contract_term/{contract_terms_version_id}/siblings")
async def o2c_contract_review_siblings_get(
    contract_terms_version_id: UUID,
    _user: Annotated[User, Depends(get_current_user)],
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    _ = _user
    return await contract_review_siblings(
        contract_terms_version_id=str(contract_terms_version_id),
        period_start=period_start,
        period_end=period_end,
    )


@router.patch("/contracts/review/contract_term/{contract_terms_version_id}/header")
async def o2c_contract_review_header_patch(
    contract_terms_version_id: UUID,
    body: ContractReviewHeaderPatchBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    fields_set = body.model_fields_set
    return await contract_review_patch_header(
        contract_terms_version_id=str(contract_terms_version_id),
        effective_from=body.effective_from,
        effective_to=body.effective_to,
        clear_effective_to=body.clear_effective_to,
        patch_effective_from="effective_from" in fields_set,
        patch_effective_to="effective_to" in fields_set,
    )


@router.patch("/contracts/review/contract_term/{contract_terms_version_id}/rate-lines")
async def o2c_contract_review_rate_lines_patch(
    contract_terms_version_id: UUID,
    body: ContractRateLineBulkPatchBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await contract_review_rate_lines_patch(
        contract_terms_version_id=str(contract_terms_version_id),
        items=list(body.items),
    )


@router.post("/contracts/review/status")
async def o2c_contract_review_status(
    body: ContractReviewStatusBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    who = (user.email or "unknown").strip()[:200] or "unknown"
    return await contract_review_set_status(
        contract_terms_version_id=str(body.contract_terms_version_id),
        status_value=(body.status or "").strip().lower(),
        updated_by=who,
        period_start=body.period_start,
        period_end=body.period_end,
    )


@router.post("/attendance-recon/resolve-and-rerun")
async def o2c_resolve_attendance_recon_and_rerun(
    body: ResolveAttendanceReconAndRerunBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    who = (user.email or "unknown").strip()[:200] or "unknown"
    return await resolve_attendance_recon_and_rerun(
        recon_id=body.recon_id,
        service_site_id=body.service_site_id,
        alias_code=body.alias_code,
        alias_display=body.alias_display,
        source_system=body.source_system,
        use_recon_period=body.use_recon_period,
        auto_run_previous_month_mis=body.auto_run_previous_month_mis,
        allow_alias_reassign=body.allow_alias_reassign,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
        resolved_by=who,
    )


@router.post("/mis/list")
async def o2c_list_mis_runs(
    body: ListMisRunsBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await list_mis_runs_with_counts(
        status=body.status, limit=body.limit, period_scope=body.period_scope
    )


@router.post("/mis/get")
async def o2c_get_mis_run(
    body: GetMisRunBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await get_mis_run_or_404(mis_run_id=body.mis_run_id)


@router.post("/mis/generate-draft")
async def o2c_generate_mis_draft(
    body: GenerateMisDraftBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await generate_mis_draft_api(
        attendance_xlsx=body.attendance_xlsx,
        client_site_key=body.client_site_key,
        period_start_s=body.period_start,
        period_end_s=body.period_end,
        out_dir=body.out_dir,
        template_path=body.template_path,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
    )


@router.post("/mis/rerun")
async def o2c_rerun_mis(
    body: RerunMisBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await rerun_mis_for_existing_run(
        mis_run_id=body.mis_run_id,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
        preserved_by=user.email,
    )


@router.post("/mis/save")
async def o2c_save_mis(
    body: SaveMisBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await save_mis_with_rerun(
        mis_run_id=body.mis_run_id,
        row_edits=body.row_edits,
        saved_by=user.email,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
    )


@router.post("/mis/approve")
async def o2c_approve_mis(
    body: ApproveMisBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await approve_mis_with_xlsx_export(
        mis_run_id=body.mis_run_id,
        approved_by=user.email,
    )


@router.post("/mis/save-and-approve")
async def o2c_save_and_approve_mis(
    body: SaveAndApproveMisBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await save_and_approve_mis_with_xlsx_export(
        mis_run_id=body.mis_run_id,
        row_edits=body.row_edits,
        approved_by=user.email,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
    )


@router.get("/site-alias/sites")
async def o2c_site_alias_list_sites(
    _user: Annotated[User, Depends(get_current_user)],
    query: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    _ = _user
    return await list_active_service_sites_for_picker(query=query, limit=limit)


@router.post("/site-alias/upsert")
async def o2c_site_alias_upsert(
    body: SiteAliasUpsertBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await site_alias_upsert_with_optional_mis_rerun(
        service_site_id=body.service_site_id,
        alias_code=body.alias_code,
        alias_display=body.alias_display,
        source_system=body.source_system,
        verified_by=(user.email or "unknown")[:200],
        auto_run_previous_month_mis=body.auto_run_previous_month_mis,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
    )


@router.get("/billing-clients")
async def o2c_list_billing_clients(
    _user: Annotated[User, Depends(get_current_user)],
    query: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    _ = _user
    return await list_billing_clients(query=query, limit=limit)


@router.post("/contracts/review/failures/reingest")
async def o2c_reingest_failed_parsing(
    _user: Annotated[User, Depends(get_current_user)],
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = _user
    body = body or {}
    return await reingest_failed_contract_parsing(
        failure_id=str(body.get("failure_id", "")).strip(),
    )


@router.post("/service-site/create")
async def o2c_create_service_site(
    body: CreateServiceSiteBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await create_service_site_with_recon(
        site_key=body.site_key,
        display_name=body.display_name,
        billing_client_id=body.billing_client_id,
        recon_id=body.recon_id,
        alias_code=body.alias_code,
        verified_by=(user.email or "unknown").strip()[:200],
        clone_rate_lines_from_site_id=body.clone_rate_lines_from_site_id,
        replace_existing_site_rate_lines=body.replace_existing_site_rate_lines,
        require_same_terms_for_period=body.require_same_terms_for_period,
        clone_terms_period_start=body.clone_terms_period_start,
        clone_terms_period_end=body.clone_terms_period_end,
        auto_run_previous_month_mis=body.auto_run_previous_month_mis,
        allow_expired_contract_terms=body.allow_expired_contract_terms,
    )


@router.post("/service-site/clone-rate-lines")
async def o2c_clone_site_rate_lines(
    body: CloneSiteRateLinesBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await clone_site_rate_lines_for_api(
        source_service_site_id=body.source_service_site_id,
        target_service_site_id=body.target_service_site_id,
        replace_existing=body.replace_existing,
        require_same_terms_for_period=body.require_same_terms_for_period,
        period_start=body.period_start,
        period_end=body.period_end,
    )


@router.post("/mis/summary-row/delete")
async def o2c_delete_mis_summary_row(
    body: DeleteMisSummaryRowBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await delete_mis_summary_row_api(
        mis_summary_row_id=body.mis_summary_row_id,
        deleted_by=user.email,
    )


@router.post("/mis/summary-row/restore")
async def o2c_restore_mis_summary_row(
    body: RestoreMisSummaryRowBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await restore_mis_summary_row_api(
        mis_summary_row_id=body.mis_summary_row_id,
        restored_by=user.email or "human",
    )


@router.post("/mis/summary-row/insert")
async def o2c_insert_mis_summary_row(
    body: InsertMisSummaryRowBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await insert_mis_summary_row_api(
        mis_run_id=body.mis_run_id,
        contract_rate_line_id=body.contract_rate_line_id,
        created_by=user.email,
    )


@router.post("/mis/summary-row/create-new")
async def o2c_create_new_mis_summary_row(
    body: CreateNewMisSummaryRowBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    who = (user.email or "").strip()[:200] or "human"
    return await create_new_mis_summary_row_api(
        mis_run_id=body.mis_run_id,
        description=body.description,
        role_code=body.role_code,
        rate_amount=body.rate_amount,
        contracted_quantity=body.contracted_quantity,
        billing_model=body.billing_model,
        visit_per_month=body.visit_per_month,
        visits_per_week=body.visits_per_week,
        invoice_admin_pct=body.invoice_admin_pct,
        invoice_admin_base=body.invoice_admin_base,
        created_by=who,
    )


@router.post("/mis/available-rate-lines")
async def o2c_available_rate_lines(
    body: AvailableRateLinesBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await list_available_rate_lines_for_mis_run(mis_run_id=body.mis_run_id)


@router.post("/mis/alternate-contract-terms")
async def o2c_alternate_contract_terms(
    body: AlternateContractTermsBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await list_alternate_contract_terms_for_mis_run(mis_run_id=body.mis_run_id)


@router.post("/mis/copy-contract-rate-lines")
async def o2c_copy_contract_rate_lines(
    body: CopyContractRateLinesBody,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _ = _user
    return await copy_contract_rate_lines_to_mis_run(
        mis_run_id=body.mis_run_id,
        source_contract_terms_version_id=body.source_contract_terms_version_id,
    )


@router.post("/mis/status")
async def o2c_set_mis_status(
    body: SetMisStatusBody,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await set_mis_status_api(
        mis_run_id=body.mis_run_id,
        status=body.status,
        actor=user.email,
        rejection_notes=body.rejection_notes,
    )
