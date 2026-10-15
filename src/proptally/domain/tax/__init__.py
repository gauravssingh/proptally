"""Tax domain module for PropTally."""
from proptally.domain.tax.depreciation import calculate_annual_depreciation, DepreciationResult
from proptally.domain.tax.schedule_e import generate_schedule_e_workpaper, export_schedule_e_csv, ScheduleEWorkpaper

__all__ = [
    "calculate_annual_depreciation",
    "DepreciationResult",
    "generate_schedule_e_workpaper",
    "export_schedule_e_csv",
    "ScheduleEWorkpaper",
]
