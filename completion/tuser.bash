_tuser_completion() {
    local cur prev cmds names
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    cmds="add useradd list users show modify usermod delete userdel login passwd env runtime proot whoami doctor paths export"
    if (( COMP_CWORD == 1 )); then
        COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )
        return
    fi
    case "${COMP_WORDS[1]}" in
        show|modify|usermod|delete|userdel|login|passwd|env|proot)
            names="$(tuser list --json 2>/dev/null | python -c 'import json,sys; print(" ".join(x["name"] for x in json.load(sys.stdin)))' 2>/dev/null)"
            COMPREPLY=( $(compgen -W "$names" -- "$cur") )
            ;;
    esac
}
complete -F _tuser_completion tuser tuseradd tuserdel tusermod tusers tlogin tsu twhoami tpasswd tuserenv tuserdoctor
