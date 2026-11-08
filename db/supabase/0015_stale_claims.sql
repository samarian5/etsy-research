-- 0015 — a keyword stuck in 'running' must not freeze the pool.
--
-- When a shard dies between claim_slice() and finish_slice() the keyword keeps
-- status='running' and last_run_at set, and the old queue_health() only counted
-- status='queued' when deciding whether anything was due. The dispatcher then
-- reported "nothing due" while three keywords sat there with a due time in the
-- past — the whole pool idled behind a dead claim.
--
-- Two changes:
--   * due = anything claimable: queued, or running with a stale heartbeat.
--     claim_slice() already accepts 'running'; this makes the health check agree.
--   * claim_slice() reaps a stale claim (no heartbeat for 45 min) by treating the
--     keyword as queued again, so the state is repaired rather than tolerated.

create or replace function public.queue_health()
returns jsonb
language sql
security definer
set search_path to 'public'
as $function$
    with b as (
        select coalesce((value ->> 'max_pages_per_day')::int, 24) as budget
          from public.settings where key = 'limits'
    ), u as (
        select coalesce(sum(greatest(0, coalesce(page_to, page_from)
                                       - coalesce(page_from, 1) + 1)), 0) as used
          from public.runs
         where started_at >= date_trunc('day', now())
    ), d as (
        -- A claim with no heartbeat for 45 minutes is dead: the runner's own
        -- watchdog gives up long before that.
        select count(*) as n,
               coalesce(sum(greatest(1, least(pages_per_run, depth - next_page + 1))), 0) as pages
          from public.keywords
         where (status = 'queued'
                or (status = 'running' and coalesce(last_run_at, now() - interval '1 day')
                                          < now() - interval '45 minutes'))
           and due_at <= now()
    ), live as (
        select worker
          from public.runs
         where status in ('running', 'queued', 'in_progress')
           and started_at > now() - interval '25 minutes'
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
$function$;

create or replace function public.claim_slice(p_pages integer default null)
returns table(keyword text, page_from integer, page_to integer, depth integer, pages integer)
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    k      public.keywords;
    pages  int;
    budget int;
    used   int;
    gap    int;
begin
    select coalesce((value ->> 'max_pages_per_day')::int, 24),
           coalesce((value ->> 'slice_gap_seconds')::int, 180)
      into budget, gap
      from public.settings where key = 'limits';
    budget := coalesce(budget, 24);
    gap    := coalesce(gap, 180);

    -- Reap dead claims first, so the pool never waits on a runner that is gone.
    update public.keywords
       set status = 'queued'
     where status = 'running'
       and coalesce(last_run_at, now() - interval '1 day') < now() - interval '45 minutes';

    select coalesce(sum(greatest(0, coalesce(r.page_to, r.page_from)
                                   - coalesce(r.page_from, 1) + 1)), 0)
      into used
      from public.runs r
     where r.started_at >= date_trunc('day', now());

    if used >= budget then
        raise notice 'daily page budget spent: % of %', used, budget;
        return;
    end if;

    select * into k
      from public.keywords
     where status in ('queued', 'running')
       and due_at <= now()
     order by due_at asc, priority desc, id asc
     limit 1
     for update skip locked;

    if not found then
        return;
    end if;

    if k.next_page > k.depth then
        update public.keywords
           set pass_no = k.pass_no + 1, next_page = 1
         where id = k.id
        returning * into k;
    end if;

    pages := greatest(1, least(coalesce(p_pages, k.pages_per_run),
                              k.depth - k.next_page + 1,
                              budget - used));

    update public.keywords
       set status      = 'running',
           last_run_at = now(),
           due_at      = now() + case
                           when k.next_page + pages - 1 < k.depth
                             then make_interval(secs => gap)
                           else make_interval(hours => k.interval_hours)
                         end
     where id = k.id;

    return query select k.keyword, k.next_page, k.next_page + pages - 1, k.depth, pages;
end;
$function$;
