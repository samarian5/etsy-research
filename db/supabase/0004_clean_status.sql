-- 0004_clean_status.sql
--
-- After every *clean* slice, finish_slice() left the keyword labelled 'running'.
-- It is claimable ('queued' and 'running' are both picked up by claim_slice), so the
-- queue never actually stallied — but the board showed a permanently "running" chip
-- for work that had long since finished, and that is the kind of state that makes a
-- queue impossible to reason about at a glance.
--
-- A keyword is 'running' only while a slice is in flight. Once the slice returns,
-- it goes back to 'queued' unless the caller says otherwise ('blocked', 'paused').

create or replace function public.finish_slice(
    p_keyword   text,
    p_next_page int,
    p_status    text default null,
    p_listings  int default 0
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.keywords
       set next_page     = greatest(1, p_next_page),
           -- p_status wins when the caller has an opinion (blocked / paused).
           -- Otherwise the slice simply handed the keyword back to the queue.
           status        = coalesce(p_status, 'queued'),
           finished_at   = now(),
           listings_seen = listings_seen + greatest(0, p_listings),
           pages_done    = pages_done + 1
     where keyword = p_keyword;
end;
$$;

revoke all on function public.finish_slice(text, int, text, int) from anon, authenticated;

-- Heal the two rows the old definition left behind.
update public.keywords
   set status = 'queued'
 where status = 'running';

comment on column public.keywords.status is
    'running = a slice is in flight right now; queued = waiting for the next claim; '
    'blocked = three consecutive failures (parked); paused = operator stopped it.';
