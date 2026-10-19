"""Tax domain module for PropTally."""
from proptally.domain.tax.depreciation import DepreciationResult, calculate_annual_depreciation
from proptally.domain.tax.schedule_e import ScheduleEWorkpaper, export_schedule_e_csv, generate_schedule_e_workpaper

__all__ = [
    "calculate_annual_depreciation",
    "DepreciationResult",
    "generate_schedule_e_workpaper",
    "export_schedule_e_csv",
    "ScheduleEWorkpaper",
]
