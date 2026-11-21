/**
 * GRPH-1812. The first navigation on a worker is Vite compiling that harness.
 * On a shared runner the transform is still running when the 5s expect default
 * gives up, so the landmark reads as missing (`expected 5 received 0`, element
 * not found). A click has no expect timeout, so it burned the whole 120s test
 * cap on run 44848425354 and the button was still not there; the next test in
 * the same file passed in about a second, once the compile had landed.
 *
 * Pass this to the assertion that the page has rendered. The count and the
 * sentence stay what they are. A landmark that never renders fails that
 * assertion — there is no retry and no second navigation.
 *
 * 150s is past that recorded hang. The test cap in these files sits past this
 * wait (`test.describe.configure`), so the cap is not what fails the assertion.
 * This is not a global expect budget: every other assertion keeps 5s.
 */
export const COMPILE = { timeout: 150_000 };
