from dataclasses import dataclass

from .models import Project, ProjectUrlGroup


@dataclass(frozen=True)
class UrlClassification:
    group: ProjectUrlGroup | None
    overlapping_groups: tuple[ProjectUrlGroup, ...]

    @property
    def has_overlap(self):
        return len(self.overlapping_groups) > 1


def classify_url(project: Project, url: str) -> UrlClassification:
    """Select the highest-priority matching group and retain overlap diagnostics."""
    groups = project.url_groups.filter(active=True).prefetch_related("rules")
    matched = [group for group in groups if any(rule.matches(url) for rule in group.rules.all())]
    matched.sort(key=lambda group: (-group.priority, str(group.id)))
    return UrlClassification(matched[0] if matched else None, tuple(matched))
