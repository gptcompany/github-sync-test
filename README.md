# GitHub Sync Test Repository

Test repository for verifying GitHub sync functionality across:
- **Speckit** framework (tasks.md → GitHub Issues)
- **GSD** framework (ROADMAP.md → GitHub Issues)

## Test Scenarios

1. Speckit taskstoissues sync
2. GSD roadmap sync (if adapter exists)
3. Bidirectional sync (closed issues → completed tasks)
4. Project board integration
5. Labels and milestones creation

## Structure

```
.specify/          # Speckit format
  spec.md
  tasks.md
.planning/         # GSD format
  ROADMAP.md
  PROJECT.md
```
