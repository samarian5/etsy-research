-- A daily ceiling on search pages, enforced where the work is handed out.
--
-- Twelve 2-hourly runs at 2 pages each is exactly 24 pages/day, which is polite
-- for one account. If a keyword is re-queued, a manual dispatch fires, or a slice
-- is retried, that number can drift upward without anyone noticing — so the budget
-- lives in the database and claim_slice() simply returns no work once it is spent.
-- Raise it with:
--   update public.settings set value = '{"max_pages_per_day": 36}' where key='limits';

insert into public.settings (key, value)
values ('limits', '{"max_pages_per_day": 24}'::jsonb)
on conflict (key) do nothing;

create or replace function public.claim_slice(p_pages int default null)
returns table (keyword text, page_from int, page_to int, depth int, pages int)
language plpgsql
security definer
set search_path = public
as $$
declare
    k      public.keywords;
    pages  int;
    budget int;
    used   int;
begin
    select coalesce((value->>'max_pages_per_day')::int, 24) into budget
      from public.settings where key = 'limits';
    budget := coalesce(budget, 24);

    select coalesce(sum(greatest(0, coalesce(r.page_to, r.page_from) - coalesce(r.page_from, 1) + 1)), 0)
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
     order by coalesce(last_run_at, 'epoch'::timestamptz) asc, priority desc, id asc
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
       set status = 'running', last_run_at = now()
     where id = k.id;

    return query select k.keyword, k.next_page, k.next_page + pages - 1, k.depth, pages;
end;
$$;
