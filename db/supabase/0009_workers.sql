-- 0009 — parallel workers
--
-- Until now the crawler was single-flight: one concurrency group, one slice at a
-- time. That was a scheduling choice, not a database constraint — claim_slice()
-- already used `for update skip locked`, which is exactly the primitive a
-- worker pool needs. This migration turns the queue into a real multi-consumer
-- queue:
--
--   * runs.worker          — which slot ran this slice (w1..wN)
--   * queue_health()       — one row of numbers for the dispatcher (the "load
--                            balancer"): budget left, keywords due, pages of
--                            demand, and which workers are currently live.
--
-- Nothing about claiming changes: two workers calling claim_slice() at the same
-- instant get two different keywords because of skip locked, and due_at is
-- pushed forward inside the same transaction, so a keyword cannot be handed out
-- twice within its interval.

alter table public.runs add column if not exists worker text;

create index if not exists runs_worker_idx on public.runs (worker, started_at desc);

comment on column public.runs.worker is
    'Worker slot that ran this slice (w1..wN). NULL = legacy single-flight runs.';

-- ───────────────────────────── load balancer ─────────────────────────────────
-- The dispatcher asks this before every fan-out: how much work is there, how
-- much budget is left, and which slots are already busy. Keeping the arithmetic
-- in SQL means the dispatcher and claim_slice() can never disagree about what
-- "due" or "over budget" means.
create or replace function public.queue_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    with b as (
        select coalesce((value ->> 'max_pages_per_day')::int, 24) as budget
          from public.settings where key = 'limits'
    ), u as (
        select coalesce(sum(greatest(0, coalesce(page_to, page_from)
                                       - coalesce(page_from, 1) + 1)), 0) as used
          from public.runs
         where started_at >= date_trunc('day', now())
    ), d as (
        select count(*) as n,
               coalesce(sum(greatest(1, least(pages_per_run, depth - next_page + 1))), 0) as pages
          from public.keywords
         where status = 'queued'
           and due_at <= now()
    ), live as (
        select worker
          from public.runs
         where status in ('queued', 'in_progress')
           and started_at > now() - interval '50 minutes'
    )
    select jsonb_build_object(
        'budget',       (select budget from b),
        'used',         (select used from u),
        'left',         greatest((select budget from b) - (select used from u), 0),
        'due_keywords', (select n from d),
        'due_pages',    (select pages from d),
        'live_runs',    (select count(*) from live),
        'busy_workers', coalesce((select jsonb_agg(distinct worker)
                                    from live where worker is not null), '[]'::jsonb),
        'at',           now()
    );
$$;

revoke execute on function public.queue_health() from public, anon, authenticated;
grant  execute on function public.queue_health() to service_role;
