-- 0022_claim_dna_scope.sql — let the DNA pass be aimed at a product family.
--
-- The phone-case push needs the artwork reader pointed at cases, not at whatever
-- happens to carry the most favourites. Adding a scope filter to the claim keeps the
-- queue a lease (design_dna_at) and the ordering untouched; it only narrows the
-- candidate set. p_scope matches listings.first_keyword with LIKE, so
-- 'phone%' covers "phone case", "phone cover" and any future case keyword.

drop function if exists public.claim_dna(integer, integer);

create or replace function public.claim_dna(
    p_limit integer default 20,
    p_cool_minutes integer default 30,
    p_scope text default null)
returns table(etsy_id text, title text, gallery jsonb, image_url text, favorites integer, price_usd numeric)
language sql security definer set search_path = public as $$
  with pick as (
    select l.etsy_id
      from public.listings l
     where (l.design_dna_at is null
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
