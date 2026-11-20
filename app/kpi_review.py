"""Per-record decisions applied to a single KPI calculation."""
from .manual import kpi_decisions, kpi_exclusions


class RecordReview:
    def __init__(self, con, emp, month, code, kind):
        self.kind = kind
        self.decisions = kpi_decisions(con, emp, month, code, kind)
        self.excluded = kpi_exclusions(con, emp, month, code, kind)
        self.rows = []

    def apply(self, ref, label, auto):
        ref = str(ref)
        value = self.decisions.get(ref)
        excluded = ref in self.excluded
        self.rows.append({"kind": self.kind, "ref": ref, "label": label or ref, "excluded": excluded,
                          "auto": None if auto is None else bool(auto), "value": value})
        if excluded:
            return False
        return auto if value is None else bool(value)

    @property
    def included_count(self):
        return sum(not row["excluded"] for row in self.rows)

    def result_label(self, ref, original):
        if str(ref) in self.excluded:
            return "Исключён из расчёта"
        value = self.decisions.get(str(ref))
        if value is None:
            return original
        return "Засчитан руководителем" if value else "Не засчитан руководителем"
