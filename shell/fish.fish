# Shared fish integration for Termux User Manager virtual accounts.
# It deliberately does not replace fish_prompt or other user UI customizations.
if status is-interactive
    if test "$TUSER_DIRENV" != 0; and type -q direnv
        direnv hook fish | source
    end
end
