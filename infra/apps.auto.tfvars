# The app list — COMMITTED on purpose.
#
# Image tags are not secrets, and keeping them out of git meant nothing recorded
# what was actually deployed: answering "which version is running?" required an
# SSH session and `docker inspect`. Secrets stay in terraform.tfvars, which is
# gitignored.
#
# Deploying a new version is a one-line diff here followed by ./deploy.sh.

apps = {
  melanzana = {
    image     = "melanzana-monitor"
    image_tag = "v2.0.2"
    memory    = "128m"
  }

  jeffco = {
    image     = "jeffco-sub-monitor"
    image_tag = "v2.0.2"
    memory    = "256m"
  }

  fashionjobs = {
    image     = "fashionjobs-monitor"
    image_tag = "v2.1.0"
    memory    = "128m"
  }
}
