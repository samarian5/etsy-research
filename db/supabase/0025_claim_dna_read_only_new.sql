-- 0025_claim_dna_read_only_new.sql — stop the artwork queue from cycling.
--
-- claim_dna only looked at design_dna_at, so a listing that had already been read
-- became claimable again as soon as its lease aged past p_cool_minutes. A long
-- `--scope 'phone%'` loop then spent most of its rounds re-reading the same rows:
-- 30 rounds x 200 claims moved the "read" count by ~1,300 instead of 6,000.
--
-- A read row is finished work. Excluding design_layout is not null makes the queue
-- monotonic; to deliberately re-read something, null its design_layout.

drop function if exists public.claim_dna(integer, integer, text);

create or replace function public.claim_dna(
    p_limit integer default 20,
    p_cool_minutes integer default 30,
    p_scope text default null)
returns table(etsy_id text, title text, gallery jsonb, image_url text, favorites integer, price_usd numeric)
language sql security definer set search_path = public as $$
  with pick as (
    select l.etsy_id
      from public.listings l
     where l.design_layout is null                       -- never re-read finished work
       and (l.design_dna_at is null
            or l.design_dna_at < now() - make_interval(mins => p_cool_minutes))
       and (p_scope is null or coalesce(l.first_keyword, '') like p_scope)
     order by coalesce(l.favorites, 0) + 40 * coalesce(l.bought_24h, 0)
              + 15 * coalesce(l.carts, 0) desc
     limit greatest(1, least(p_limit, 200))
       for update skip locked
  ), claimed as (
    update public.listings l
       set design_dna_at = now()
      from pick
     where l.etsy_id = pick.etsy_id
    returning l.etsy_id, l.title, l.gallery, l.image_url, l.favorites, l.price_usd
  )
  select * from claimed;
$$;

grant execute on function public.claim_dna(integer, integer, text) to authenticated;
revoke all on function public.claim_dna(integer, integer, text) from anon;
