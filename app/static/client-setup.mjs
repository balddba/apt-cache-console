/** Generate shell commands only from a validated, canonical HTTP proxy URL. */
export function clientSetupCommands(value) {
  let url;
  try { url = new URL(value); }
  catch { throw new Error('Enter a valid HTTP proxy URL, for example http://192.168.1.10:8000/.'); }
  if (url.protocol !== 'http:' || !url.hostname ||
      !/^[a-zA-Z0-9.:[\]-]+$/.test(url.hostname) ||
      /[\s\\]/.test(value) || url.username || url.password ||
      url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Use an HTTP URL with a host and optional port, without credentials, a path, query, or fragment.');
  }
  const proxy = `${url.origin}/`;
  return {
    setup: `printf '%s\\n' 'Acquire::http::Proxy "${proxy}";' | sudo tee /etc/apt/apt.conf.d/01squid-deb-proxy > /dev/null\nif grep -Rqs 'https://download.docker.com/linux/ubuntu' /etc/apt/sources.list /etc/apt/sources.list.d; then\n  sudo install -m 0755 -d /etc/apt/keyrings &&\n  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc &&\n  sudo chmod a+r /etc/apt/keyrings/docker.asc\nfi &&\nsudo apt-get update`,
    verify: "apt-config dump | grep -F 'Acquire::http::Proxy'",
    remove: 'sudo rm -f /etc/apt/apt.conf.d/01squid-deb-proxy',
  };
}
