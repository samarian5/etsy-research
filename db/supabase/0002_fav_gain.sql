-- Momentum as a stored column.
--
-- PostgREST cannot compare two columns in a filter (`favorites > prev_favorites`
-- is not expressible in a query string), and the board's "Rising" tab is exactly
-- that comparison. A generated column turns it into a plain integer filter and
-- makes it sortable, with no view or RPC in the read path.

alter table public.listings
    add column if not exists fav_gain int
    generated always as (coalesce(favorites, 0) - coalesce(prev_favorites, favorites, 0)) stored;

create index if not exists listings_fav_gain_idx on public.listings (fav_gain desc nulls last);
