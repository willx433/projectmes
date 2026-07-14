# Escalations — Atlas MES

Protocol (IMPLEMENTATION_PLAN.md §2): a Sonnet/Haiku agent that hits an **architectural
question** mid-task stops, appends an entry below, marks its task blocked, and hands off.
Fable answers in-place. If the answer deviates from the design docs, Fable also files a CR
in `docs/CHANGE_REQUESTS.md`. Cheap tiers never improvise architecture.

## Entry template

```
### ESC-NNN — <one-line question>
- Date / Task ID / Agent tier:
- Context: <what was being built, what the docs say, why it's ambiguous>
- Options considered: <A / B, with one-line trade-off each>
- BLOCKED files: <paths>
- Fable resolution: <answer + rationale + CR-ID if a deviation>
- Status: open | resolved
```

---

*(no escalations yet)*
