-- 0007_verify_token.sql
--
-- Why: the board is a public static page. Everything that changes state goes
-- through a token-gated RPC, which is correct — but the browser had no way to
-- tell "you typed it wrong" from "the token was rotated", so it re-prompted
-- blindly and the user could not know whether to retry or go and look it up.
--
-- verify_token() answers that question without touching any data, so the UI can
-- validate at the moment of entry instead of failing later inside a real write.

create or replace function public.verify_token(p_token text)
returns boolean
language sql
stable
security definer
set search_path = public, extensions
as $$
  select coalesce(
    (select (value->>'sha256') = encode(extensions.digest(coalesce(p_token, ''), 'sha256'), 'hex')
       from public.settings
      where key = 'write_token'),
    false
  );
$$;

revoke all on function public.verify_token(text) from public;
grant execute on function public.verify_token(text) to anon, authenticated;

-- Never return the hash, only a yes/no about the token the caller already holds.
comment on function public.verify_token(text) is
  'True when p_token matches the stored SHA-256 of the write token. Reveals nothing else.';
