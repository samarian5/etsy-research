-- 0013 — let several workers crawl the SAME keyword at once.
--
-- Before this, a slice pushed due_at a full interval ahead, so one keyword could
-- only ever be crawled once per interval — no matter how many workers were idle.
-- With depth 60 that is a 60-hour lap on a 2-hour interval, which makes a bigger
-- worker pool pointless.
--
-- The rule now:
--   * still pages left in the current lap  -> come back in `slice_gap_seconds`
--   * lap finished (next_page > depth)     -> wait the keyword's own interval
--
-- So `interval_hours` becomes "how often a keyword is revisited", and the gap is
-- "how closely two slices of the same lap may follow each other". claim_slice's
-- `for update skip locked` still guarantees two workers never take the same page
-- range, and the page budget is still enforced inside the same function.

insert into public.settings (key, value)
values ('limits', jsonb_build_object('max_pages_per_day', 320, 'slice_gap_seconds', 180))
on conflict (key) do nothing;

update public.settings
   set value = value || jsonb_build_object('slice_gap_seconds',
                                           coalesce((value ->> 'slice_gap_seconds')::int, 180))
 where key = 'limits';

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
       and due_at <= now()                       -- not due yet = not our problem
     order by due_at asc, priority desc, id asc   -- most overdue first, then who matters
     limit 1
     for update skip locked;

    if not found then
        return;                                   -- nothing is due right now
    end if;

    -- a finished pass rolls over to page 1 of the next pass
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
           -- Push the due time forward now, not only on finish: if this runner is
           -- killed mid-crawl, finish_slice() never runs and the keyword would
           -- otherwise be claimable the very next tick, every tick, forever.
           due_at      = now() + case
                           when k.next_page + pages - 1 < k.depth
                             then make_interval(secs => gap)      -- lap still open
                           else make_interval(hours => k.interval_hours)
                         end
     where id = k.id;

    return query select k.keyword, k.next_page, k.next_page + pages - 1, k.depth, pages;
end;
$function$;

create or replace function public.finish_slice(p_keyword text, p_next_page integer,
                                                p_status text default null, p_listings integer default 0)
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    gap int;
begin
    select coalesce((value ->> 'slice_gap_seconds')::int, 180) into gap
      from public.settings where key = 'limits';
    gap := coalesce(gap, 180);

    update public.keywords
       set next_page     = greatest(1, p_next_page),
           status        = coalesce(p_status, 'queued'),
           -- Inside a lap, keep going soon; after the last page, wait the interval.
           due_at        = now() + case
                             when greatest(1, p_next_page) <= depth
                               then make_interval(secs => gap)
                             else make_interval(hours => interval_hours)
                           end,
           finished_at   = now(),
           listings_seen = listings_seen + greatest(0, p_listings),
           pages_done    = pages_done + 1
     where keyword = p_keyword;
end;
$function$;

-- The two keywords this build is about get first claim on an idle worker.
update public.keywords set priority = 10 where keyword in ('tshirt', 'mom tshirt');
update public.keywords set priority = 0  where keyword not in ('tshirt', 'mom tshirt');
