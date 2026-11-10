-- 0024_signals_matview.sql — the signals panel was computing over every title on
-- every request, and PostgREST's statement timeout started killing it once the corpus
-- passed ~15k listings (HTTP 500 on phrase_stats and keyword_entry, which blanked the
-- Signals panel and, on a bad load, took the phone-case briefs down with it).
--
-- Same fix as listing_niche_mv (0018): compute once, refresh after each clean slice,
-- and let the browser read a table with an index. The underlying views stay as the
-- definition of record.

drop materialized view if exists public.phrase_stats_mv cascade;
create materialized view public.phrase_stats_mv as
select * from public.phrase_stats;

create unique index if not exists phrase_stats_mv_pk
    on public.phrase_stats_mv (keyword, phrase);
create index if not exists phrase_stats_mv_demand
    on public.phrase_stats_mv (keyword, median_favorites desc nulls last);

drop materialized view if exists public.keyword_entry_mv cascade;
create materialized view public.keyword_entry_mv as
select * from public.keyword_entry;

create unique index if not exists keyword_entry_mv_pk
    on public.keyword_entry_mv (keyword);
create index if not exists keyword_entry_mv_score
    on public.keyword_entry_mv (entry_score desc nulls last);

create or replace function public.refresh_signals_views() returns void
language plpgsql security definer set search_path = public as $$
begin
  begin
    refresh materialized view concurrently public.phrase_stats_mv;
  exception when others then
    refresh materialized view public.phrase_stats_mv;      -- first run, no unique index yet
  end;
  begin
    refresh materialized view concurrently public.keyword_entry_mv;
  exception when others then
    refresh materialized view public.keyword_entry_mv;
  end;
end $$;

grant select on public.phrase_stats_mv, public.keyword_entry_mv to authenticated;
revoke all on public.phrase_stats_mv, public.keyword_entry_mv from anon;
grant execute on function public.refresh_signals_views() to authenticated;
