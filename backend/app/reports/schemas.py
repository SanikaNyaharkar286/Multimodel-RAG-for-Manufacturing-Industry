from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

ReviewStatus = Literal['new', 'under_review', 'reviewed']


class SubmitReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_status: ReviewStatus
    admin_comment: str | None = Field(default=None, max_length=10000)
    version: int = Field(ge=1)


class Submission(BaseModel):
    submission_id: str
    report_id: str
    report_number: str
    technician_name: str = ''
    technician_email: str = ''
    machine_name: str = ''
    machine_model: str = ''
    issue_description: str = ''
    session_id: str = ''
    case_id: str = ''
    report_type: str
    revision: int
    review_status: ReviewStatus
    admin_comment: str | None = None
    assigned_admin_uid: str | None = None
    email_status: Literal['pending', 'sending', 'sent', 'failed']
    email_delivery: str = 'not_confirmed'
    email_error: str = ''
    created_at: datetime
    submitted_at: datetime
    reviewed_at: datetime | None = None
    updated_at: datetime
    version: int = 1


class ReportList(BaseModel):
    reports: list[Submission]
    total: int
    counts: dict[str, int]
    page: int
    page_size: int


class Notification(BaseModel):
    notification_id: str
    type: Literal['maintenance_report_submitted', 'registration_request']
    title: str
    message: str
    report_id: str = ''
    request_uid: str = ''
    is_read: bool
    created_at: datetime
    read_at: datetime | None = None


class NotificationList(BaseModel):
    notifications: list[Notification]
    unread_count: int
    page: int
    page_size: int
