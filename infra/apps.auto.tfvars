# The app list — COMMITTED on purpose.
#
# Image tags are not secrets, and keeping them out of git meant nothing recorded
# what was actually deployed: answering "which version is running?" required an
# SSH session and `docker inspect`. Secrets stay in terraform.tfvars, which is
# gitignored.
#
# Deploying a new version is a one-line diff here followed by ./deploy.sh.

# Non-secret production safety gate. Keep false for the first deployment of an
# auto-accept-capable image, verify health, then enable in a separate deployment.
jeffco_auto_accept_enabled = true

apps = {
  melanzana = {
    image     = "melanzana-monitor"
    image_tag = "v2.6.0"
    memory    = "128m"
  }

  jeffco = {
    image     = "jeffco-sub-monitor"
    image_tag = "v2.6.0"
    memory    = "256m"
  }

  # Mom's instance: the jeffco image from the jeffco repository, so one push
  # serves both. Rolling the tag back below v2.5.0 must remove this entry in the
  # same change — an older image ignores JEFFCO_PROFILE and would run Dad's
  # high-school filter on her account.
  "jeffco-mom" = {
    image      = "jeffco-sub-monitor"
    repository = "jeffco"
    image_tag  = "v2.6.0"
    memory     = "256m"
  }

  fashionjobs = {
    image     = "fashionjobs-monitor"
    image_tag = "v2.6.0"
    memory    = "128m"
  }
}
