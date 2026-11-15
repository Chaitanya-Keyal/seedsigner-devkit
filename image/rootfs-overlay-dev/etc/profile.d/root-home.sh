# The console shells are spawned by init with HOME=/, so a login bash there looks for
# /.bash_profile and never reads /root's dotfiles. bash expands ~/.bash_profile after
# /etc/profile, so fixing HOME here is enough.
if [ "$HOME" = / ] && [ "$(id -u)" = 0 ]; then
  export HOME=/root
  cd /root
fi
