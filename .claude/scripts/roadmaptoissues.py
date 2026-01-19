#!/usr/bin/env python3
"""GSD ROADMAP.md <-> GitHub Issues synchronization.

Syncs GSD project artifacts to GitHub:
- Phases → GitHub Milestones
- Plans → GitHub Issues (linked to phase milestone)
- Todos → GitHub Issues (with 'todo' label)

Supports bidirectional sync:
- Completed plans (marked [x]) → Close issues
- Closed issues → Mark plans complete in ROADMAP.md

Usage:
    # Create issues from ROADMAP.md
    python roadmaptoissues.py --roadmap .planning/ROADMAP.md

    # Create issues and link to project board
    python roadmaptoissues.py --roadmap .planning/ROADMAP.md --auto-project

    # Also sync todos from .planning/todos/
    python roadmaptoissues.py --roadmap .planning/ROADMAP.md --sync-todos

    # Bidirectional sync (update ROADMAP from closed issues)
    python roadmaptoissues.py --sync .planning

    # Dry run
    python roadmaptoissues.py --roadmap .planning/ROADMAP.md --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Import shared functions from github_sync_core
try:
    from github_sync_core import (
        run_gh_command,
        get_repo_info,
        ensure_labels_exist,
        ensure_project_exists,
        add_issue_to_project,
        get_issue_node_id,
        ensure_milestone_exists,
        get_existing_milestones,
        get_existing_issues,
        close_issue,
    )

    try:
        from github_sync_core import get_project_by_name
    except ImportError:
        get_project_by_name = None
except ImportError:
    print("Error: github_sync_core.py not found. Ensure it's in the same directory.")
    sys.exit(1)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class Plan:
    """Represents a single plan from ROADMAP.md."""

    id: str  # e.g., "01-02" or "02.1-01"
    phase_num: str  # e.g., "1" or "2.1"
    plan_num: str  # e.g., "02"
    description: str
    status: str  # "pending" or "completed"
    line_number: int = 0
    line_text: str = ""


@dataclass
class Phase:
    """Represents a phase from ROADMAP.md."""

    number: str  # e.g., "1" or "2.1"
    name: str
    goal: str = ""
    depends_on: str = ""
    requirements: list[str] = field(default_factory=list)
    research: str = ""
    plans: list[Plan] = field(default_factory=list)
    status: str = "pending"  # "pending", "in_progress", "completed"
    line_number: int = 0
    line_text: str = ""


@dataclass
class Todo:
    """Represents a GSD todo file."""

    filename: str
    title: str
    area: str
    created: str
    problem: str = ""
    solution: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    """Results from sync operation."""

    milestones_created: int = 0
    milestones_existing: int = 0
    issues_created: int = 0
    issues_existing: int = 0
    issues_closed: int = 0
    plans_marked_complete: int = 0
    todos_synced: int = 0
    errors: list[str] = field(default_factory=list)


# =============================================================================
# Helpers
# =============================================================================


def _normalize_phase(n: str) -> str:
    """Normalize phase number for comparison: '01' -> '1', '02.1' -> '2.1'"""
    parts = n.split(".")
    parts[0] = str(int(parts[0]))
    return ".".join(parts)


# =============================================================================
# Parsing
# =============================================================================


def parse_roadmap(file_path: Path) -> tuple[list[Phase], list[Plan]]:
    """Parse ROADMAP.md and extract phases and plans."""
    content = file_path.read_text()
    lines = content.split("\n")

    phases: list[Phase] = []
    all_plans: list[Plan] = []
    current_phase: Phase | None = None
    in_phase_details = False

    # Phase patterns
    # From Phases section: - [ ] **Phase 1: Name** - Description
    phase_checkbox_pattern = re.compile(
        r"^\s*-\s*\[([ xX])\]\s*\*\*Phase\s+(\d+(?:\.\d+)?)\s*:\s*(.+?)\*\*\s*(?:-\s*(.+))?$"
    )
    # From Phase Details section: ### Phase 1: Name
    phase_header_pattern = re.compile(r"^###\s*Phase\s+(\d+(?:\.\d+)?)\s*:\s*(.+)$")

    # Plan pattern: - [ ] 01-02: Description
    plan_pattern = re.compile(
        r"^\s*-\s*\[([ xX])\]\s*(\d+(?:\.\d+)?)-(\d+)\s*:\s*(.+)$"
    )

    # Phase details patterns
    goal_pattern = re.compile(r"^\*\*Goal\*\*:\s*(.+)$")
    depends_pattern = re.compile(r"^\*\*Depends on\*\*:\s*(.+)$")
    requirements_pattern = re.compile(r"^\*\*Requirements\*\*:\s*(.+)$")
    research_pattern = re.compile(r"^\*\*Research\*\*:\s*(.+)$")

    for i, line in enumerate(lines, 1):
        # Check for Phase Details section
        if "## Phase Details" in line:
            in_phase_details = True
            continue

        # Parse phase from Phase Details section (more detailed)
        phase_header_match = phase_header_pattern.match(line)
        if phase_header_match and in_phase_details:
            phase_num = phase_header_match.group(1)
            phase_name = phase_header_match.group(2).strip()

            # Find or create phase
            existing = next((p for p in phases if p.number == phase_num), None)
            if existing:
                current_phase = existing
            else:
                current_phase = Phase(
                    number=phase_num,
                    name=phase_name,
                    line_number=i,
                    line_text=line,
                )
                phases.append(current_phase)
            continue

        # Parse phase from Phases checklist (simpler)
        phase_checkbox_match = phase_checkbox_pattern.match(line)
        if phase_checkbox_match and not in_phase_details:
            status = (
                "completed"
                if phase_checkbox_match.group(1).upper() == "X"
                else "pending"
            )
            phase_num = phase_checkbox_match.group(2)
            phase_name = phase_checkbox_match.group(3).strip()

            # Check if phase already exists from details section
            existing = next((p for p in phases if p.number == phase_num), None)
            if existing:
                existing.status = status
                existing.line_number = i
                existing.line_text = line
            else:
                current_phase = Phase(
                    number=phase_num,
                    name=phase_name,
                    status=status,
                    line_number=i,
                    line_text=line,
                )
                phases.append(current_phase)
            continue

        # Parse phase details
        if current_phase and in_phase_details:
            goal_match = goal_pattern.match(line)
            if goal_match:
                current_phase.goal = goal_match.group(1)
                continue

            depends_match = depends_pattern.match(line)
            if depends_match:
                current_phase.depends_on = depends_match.group(1)
                continue

            requirements_match = requirements_pattern.match(line)
            if requirements_match:
                reqs = requirements_match.group(1)
                current_phase.requirements = [
                    r.strip() for r in re.findall(r"REQ-\d+", reqs)
                ]
                continue

            research_match = research_pattern.match(line)
            if research_match:
                current_phase.research = research_match.group(1)
                continue

        # Parse plans
        plan_match = plan_pattern.match(line)
        if plan_match:
            status = "completed" if plan_match.group(1).upper() == "X" else "pending"
            phase_num = plan_match.group(2)
            plan_num = plan_match.group(3)
            description = plan_match.group(4).strip()

            plan_id = f"{phase_num.replace('.', '')}-{plan_num}"

            plan = Plan(
                id=plan_id,
                phase_num=phase_num,
                plan_num=plan_num,
                description=description,
                status=status,
                line_number=i,
                line_text=line,
            )
            all_plans.append(plan)

            normalized_phase_num = _normalize_phase(phase_num)

            # Associate with current phase
            if (
                current_phase
                and _normalize_phase(current_phase.number) == normalized_phase_num
            ):
                current_phase.plans.append(plan)
            else:
                # Find the matching phase
                phase = next(
                    (
                        p
                        for p in phases
                        if _normalize_phase(p.number) == normalized_phase_num
                    ),
                    None,
                )
                if phase:
                    phase.plans.append(plan)

    return phases, all_plans


def parse_todos(todos_dir: Path) -> list[Todo]:
    """Parse todo files from .planning/todos/pending/."""
    todos: list[Todo] = []
    pending_dir = todos_dir / "pending"

    if not pending_dir.exists():
        return todos

    for todo_file in pending_dir.glob("*.md"):
        content = todo_file.read_text()

        # Parse YAML frontmatter
        frontmatter_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not frontmatter_match:
            continue

        frontmatter = frontmatter_match.group(1)

        # Extract fields
        title_match = re.search(r"^title:\s*(.+)$", frontmatter, re.MULTILINE)
        area_match = re.search(r"^area:\s*(.+)$", frontmatter, re.MULTILINE)
        created_match = re.search(r"^created:\s*(.+)$", frontmatter, re.MULTILINE)
        files_match = re.search(
            r"^files:\n((?:\s+-\s+.+\n?)+)", frontmatter, re.MULTILINE
        )

        if not title_match:
            continue

        # Extract problem section
        problem_match = re.search(r"## Problem\n\n(.+?)(?=\n## |$)", content, re.DOTALL)
        solution_match = re.search(
            r"## Solution\n\n(.+?)(?=\n## |$)", content, re.DOTALL
        )

        files = []
        if files_match:
            files = [
                f.strip()
                for f in re.findall(
                    r"^\s+-\s+(.+)$", files_match.group(1), re.MULTILINE
                )
            ]

        todo = Todo(
            filename=todo_file.name,
            title=title_match.group(1).strip(),
            area=area_match.group(1).strip() if area_match else "general",
            created=created_match.group(1).strip() if created_match else "",
            problem=problem_match.group(1).strip() if problem_match else "",
            solution=solution_match.group(1).strip() if solution_match else "",
            files=files,
        )
        todos.append(todo)

    return todos


# =============================================================================
# Issue Creation
# =============================================================================


def create_plan_issue(
    plan: Plan,
    phase: Phase,
    milestone_title: str | None,
    project_id: str | None = None,
    dry_run: bool = False,
) -> int | None:
    """Create a GitHub issue for a GSD plan."""
    title = f"[Plan-{plan.id}] {plan.description}"

    body_parts = [
        "## Plan Details",
        "",
        f"**Plan ID**: {plan.id}",
        f"**Phase**: {phase.number} - {phase.name}",
    ]

    if phase.goal:
        body_parts.extend(["", f"**Phase Goal**: {phase.goal}"])

    if phase.requirements:
        body_parts.extend(["", f"**Requirements**: {', '.join(phase.requirements)}"])

    body_parts.extend(
        [
            "",
            "### Source",
            f"- Roadmap: `.planning/ROADMAP.md` (line {plan.line_number})",
            f"- Plan file: `.planning/phases/{plan.phase_num.zfill(2)}-*/plans/{plan.id}-PLAN.md`",
            "",
            "---",
            "*Auto-generated by GSD → GitHub sync*",
        ]
    )
    body = "\n".join(body_parts)

    labels = ["auto-generated", "gsd-plan", f"phase-{phase.number}"]

    if dry_run:
        print(f"[DRY RUN] Would create issue: {title}")
        if project_id:
            print("[DRY RUN] Would link to project")
        return None

    # Ensure labels exist
    ensure_labels_exist(labels, dry_run)

    cmd = ["issue", "create", "--title", title, "--body", body]
    for label in labels:
        cmd.extend(["--label", label])
    if milestone_title:
        cmd.extend(["--milestone", milestone_title])

    code, stdout, stderr = run_gh_command(cmd, check=False)
    if code != 0:
        print(f"Failed to create issue for Plan-{plan.id}: {stderr}")
        return None

    match = re.search(r"/issues/(\d+)", stdout)
    if not match:
        return None

    issue_num = int(match.group(1))

    # Link to project using GraphQL API
    if project_id:
        issue_node_id = get_issue_node_id(issue_num)
        if issue_node_id:
            item_id = add_issue_to_project(project_id, issue_node_id, dry_run)
            if item_id:
                print("  Linked to project")

    return issue_num


def create_todo_issue(
    todo: Todo,
    project_id: str | None = None,
    dry_run: bool = False,
) -> int | None:
    """Create a GitHub issue for a GSD todo."""
    title = f"[Todo] {todo.title}"

    body_parts = [
        "## Todo Details",
        "",
        f"**Area**: {todo.area}",
        f"**Created**: {todo.created}",
    ]

    if todo.files:
        body_parts.extend(["", "### Related Files"])
        for f in todo.files:
            body_parts.append(f"- `{f}`")

    if todo.problem:
        body_parts.extend(["", "### Problem", "", todo.problem])

    if todo.solution and todo.solution != "TBD":
        body_parts.extend(["", "### Solution Hints", "", todo.solution])

    body_parts.extend(
        [
            "",
            "### Source",
            f"- Todo file: `.planning/todos/pending/{todo.filename}`",
            "",
            "---",
            "*Auto-generated by GSD → GitHub sync*",
        ]
    )
    body = "\n".join(body_parts)

    labels = ["auto-generated", "todo", f"area-{todo.area}"]

    if dry_run:
        print(f"[DRY RUN] Would create issue: {title}")
        return None

    # Ensure labels exist
    ensure_labels_exist(labels, dry_run)

    cmd = ["issue", "create", "--title", title, "--body", body]
    for label in labels:
        cmd.extend(["--label", label])

    code, stdout, stderr = run_gh_command(cmd, check=False)
    if code != 0:
        print(f"Failed to create issue for todo {todo.title}: {stderr}")
        return None

    match = re.search(r"/issues/(\d+)", stdout)
    if not match:
        return None

    issue_num = int(match.group(1))

    # Link to project using GraphQL API
    if project_id:
        issue_node_id = get_issue_node_id(issue_num)
        if issue_node_id:
            add_issue_to_project(project_id, issue_node_id, dry_run)

    return issue_num


# =============================================================================
# Sync Operations
# =============================================================================


def sync_roadmap_to_github(
    roadmap_path: Path,
    project_name: str | None = None,
    create_project: bool = False,
    sync_todos: bool = False,
    dry_run: bool = False,
) -> SyncResult:
    """Sync ROADMAP.md to GitHub issues."""
    result = SyncResult()

    # Parse roadmap
    phases, plans = parse_roadmap(roadmap_path)
    print(f"Found {len(phases)} phases and {len(plans)} plans\n")

    # Resolve project
    project_id: str | None = None
    if project_name:
        repo_info = get_repo_info()
        if repo_info:
            owner, _ = repo_info
            if create_project:
                project_info = ensure_project_exists(owner, project_name, dry_run)
            elif get_project_by_name:
                project_info = get_project_by_name(owner, project_name)
            else:
                project_info = None

            if project_info:
                project_id = project_info.id
                print(f"Project: {project_info.title} (#{project_info.number})\n")
            elif not dry_run:
                print(
                    f"Warning: Project '{project_name}' not found. Use --create-project to create it.\n"
                )

    # Get existing issues to avoid duplicates
    existing_issues = get_existing_issues("gsd-plan")
    existing_todo_issues = get_existing_issues("todo")

    # Create milestones for phases
    # First get existing milestones to track created vs existing
    existing_milestones = get_existing_milestones()
    # Map phase.number -> milestone TITLE (gh cli requires title, not number)
    milestone_map: dict[str, str | None] = {}
    for phase in phases:
        milestone_title = f"Phase {phase.number}: {phase.name}"
        was_existing = milestone_title in existing_milestones
        milestone_num = ensure_milestone_exists(
            milestone_title,
            phase.goal or f"GSD Phase {phase.number}",
            dry_run,
        )
        if milestone_num:
            milestone_map[phase.number] = milestone_title  # Store title, not number
            if was_existing:
                result.milestones_existing += 1
            else:
                result.milestones_created += 1
        # Note: milestone_num being None means failure, not counted as existing

    print()

    # Create issues for plans
    for plan in plans:
        if plan.status == "completed":
            continue

        issue_key = f"Plan-{plan.id}"
        if issue_key in existing_issues:
            result.issues_existing += 1
            print(
                f"Issue exists for {issue_key}: #{existing_issues[issue_key]['number']}"
            )
            continue

        # Find phase for this plan (normalize for comparison)
        normalized = _normalize_phase(plan.phase_num)
        phase = next(
            (p for p in phases if _normalize_phase(p.number) == normalized), None
        )
        if not phase:
            result.errors.append(f"No phase found for plan {plan.id}")
            continue

        milestone_title = milestone_map.get(phase.number)
        issue_num = create_plan_issue(plan, phase, milestone_title, project_id, dry_run)

        if issue_num or dry_run:
            result.issues_created += 1
            print(f"Created issue for Plan-{plan.id}: #{issue_num}")

    # Sync todos if requested
    if sync_todos:
        todos_dir = roadmap_path.parent / "todos"
        if todos_dir.exists():
            todos = parse_todos(todos_dir)
            print(f"\nFound {len(todos)} pending todos\n")

            for todo in todos:
                # Check for existing issue
                todo_key = f"Todo-{todo.filename}"
                if any(
                    todo.title in issue.get("title", "")
                    for issue in existing_todo_issues.values()
                ):
                    print(f"Todo issue exists: {todo.title}")
                    continue

                issue_num = create_todo_issue(todo, project_id, dry_run)
                if issue_num or dry_run:
                    result.todos_synced += 1
                    print(f"Created issue for todo: {todo.title} (#{issue_num})")

    return result


def sync_bidirectional(planning_dir: Path, dry_run: bool = False) -> SyncResult:
    """Bidirectional sync: completed plans <-> closed issues."""
    result = SyncResult()
    roadmap_path = planning_dir / "ROADMAP.md"

    if not roadmap_path.exists():
        result.errors.append(f"No ROADMAP.md in {planning_dir}")
        return result

    # Parse roadmap
    _, plans = parse_roadmap(roadmap_path)
    existing_issues = get_existing_issues("gsd-plan")

    # Build lookup
    plans_by_id = {f"Plan-{p.id}": p for p in plans}

    # Completed plans -> Close issues
    for plan in plans:
        issue_key = f"Plan-{plan.id}"
        if plan.status == "completed" and issue_key in existing_issues:
            issue = existing_issues[issue_key]
            if issue["state"] == "OPEN":
                if not dry_run:
                    close_issue(issue["number"], dry_run)
                result.issues_closed += 1
                print(f"Closed issue #{issue['number']} (Plan-{plan.id})")

    # Closed issues -> Mark plans complete
    content = roadmap_path.read_text()
    modified = False

    for issue_key, issue in existing_issues.items():
        if issue["state"] == "CLOSED" and issue_key in plans_by_id:
            plan = plans_by_id[issue_key]
            if plan.status != "completed":
                old_line = plan.line_text
                new_line = old_line.replace("- [ ]", "- [x]", 1)
                if old_line in content:
                    content = content.replace(old_line, new_line)
                    result.plans_marked_complete += 1
                    modified = True
                    print(f"Marked [x]: Plan-{plan.id}")

    if modified and not dry_run:
        roadmap_path.write_text(content)

    return result


# =============================================================================
# Main
# =============================================================================


def get_default_project_name() -> str | None:
    """Get default project board name based on repo name."""
    repo_info = get_repo_info()
    if repo_info:
        _, repo_name = repo_info
        return f"{repo_name} Development"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GSD ROADMAP.md <-> GitHub Issues synchronization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create issues from ROADMAP.md
  python roadmaptoissues.py --roadmap .planning/ROADMAP.md

  # Create issues and link to auto-derived project board
  python roadmaptoissues.py --roadmap .planning/ROADMAP.md --auto-project

  # Create project if missing, then link issues
  python roadmaptoissues.py --roadmap .planning/ROADMAP.md --auto-project --create-project

  # Also sync todos
  python roadmaptoissues.py --roadmap .planning/ROADMAP.md --sync-todos

  # Bidirectional sync
  python roadmaptoissues.py --sync .planning

  # Preview changes
  python roadmaptoissues.py --roadmap .planning/ROADMAP.md --dry-run
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--roadmap", type=Path, help="Create issues from ROADMAP.md file"
    )
    group.add_argument(
        "--sync", type=Path, help="Bidirectional sync for .planning directory"
    )

    parser.add_argument(
        "--project",
        type=str,
        help="GitHub Project board to link issues to",
    )
    parser.add_argument(
        "--auto-project",
        action="store_true",
        help="Auto-derive project name from repo ('{repo_name} Development')",
    )
    parser.add_argument(
        "--create-project",
        action="store_true",
        help="Create the project board if it doesn't exist",
    )
    parser.add_argument(
        "--sync-todos",
        action="store_true",
        help="Also sync todos from .planning/todos/",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without changes"
    )

    args = parser.parse_args()

    # Resolve project name
    project_name = args.project
    if args.auto_project and not project_name:
        project_name = get_default_project_name()
        if not project_name:
            print("Warning: Could not auto-detect project name (not in a git repo?)")

    result = SyncResult()

    if args.roadmap:
        # Create issues mode
        if not args.roadmap.exists():
            print(f"Error: ROADMAP.md not found: {args.roadmap}")
            sys.exit(1)

        print(f"Syncing: {args.roadmap}")
        print(f"Dry run: {args.dry_run}\n")

        if project_name:
            print(f"Project board: {project_name}")
            if args.create_project:
                print("  (will create if missing)")
            print()

        result = sync_roadmap_to_github(
            args.roadmap,
            project_name,
            args.create_project,
            args.sync_todos,
            args.dry_run,
        )

    elif args.sync:
        # Bidirectional sync mode
        if not args.sync.exists():
            print(f"Error: .planning directory not found: {args.sync}")
            sys.exit(1)

        print(f"Bidirectional sync: {args.sync}")
        print(f"Dry run: {args.dry_run}\n")
        result = sync_bidirectional(args.sync, args.dry_run)

    # Print summary
    print("\n=== Summary ===")
    if args.roadmap:
        print(f"Milestones created: {result.milestones_created}")
        print(f"Milestones existing: {result.milestones_existing}")
        print(f"Issues created: {result.issues_created}")
        print(f"Issues existing: {result.issues_existing}")
        if args.sync_todos:
            print(f"Todos synced: {result.todos_synced}")
    else:
        print(f"Issues closed: {result.issues_closed}")
        print(f"Plans marked [x]: {result.plans_marked_complete}")

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors:
            print(f"  - {err}")

    if args.dry_run:
        print("\n[DRY RUN] No changes applied.")


if __name__ == "__main__":
    main()
