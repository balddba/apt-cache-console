/* Credentials are sent only to the same-origin login endpoint. */
document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('sign-in');
  const error = document.getElementById('login-error');
  button.disabled = true;
  button.textContent = 'Signing in…';
  error.hidden = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'squid-ui' },
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      }),
    });
    if (!response.ok) throw new Error(response.status === 401
      ? 'Invalid username or password.' : 'Unable to sign in. Please try again.');
    window.location.replace('/');
  } catch (failure) {
    error.textContent = failure instanceof TypeError ? 'Unable to connect. Please try again.' : failure.message;
    error.hidden = false;
    document.getElementById('password').focus();
  } finally {
    button.disabled = false;
    button.textContent = 'Sign in';
  }
});
