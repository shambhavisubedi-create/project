# GoGather: Feature Ideas and Development Roadmap

Prepared on 12 September 2026 from a review of `app.py`, `SYSTEM_AUDIT.md`, and the project documentation. This is a proposal; the features below have not been implemented as part of this review. Existing audit test results were not rerun.

## 1. What your system already does

GoGather already lets travellers explore destinations, search packages, book trips, pay through eSewa or Stripe, create or join travel groups, use group chat, update profiles, and receive in-app notifications. Admins can manage travellers, destinations, packages, bookings, groups, and reports.

You already have basic package filters, itinerary information, group member limits, and booking cancellation handling. The suggestions below extend those features instead of rebuilding them.

## 2. Features you can add

Effort is relative: Small means a focused addition, Medium means changes across several screens and database records, and Large means a workflow with more rules or external services.

| No. | Feature | What it means in everyday language | Why it helps | Priority | Effort |
|---|---|---|---|---|---|
| 1 | Forgot password | A traveller receives a secure email link to set a new password. | People can recover their own accounts. | High | Medium |
| 2 | Booking emails and trip reminders | Send an email when a booking is confirmed, payment succeeds, or a trip is approaching. | Travellers stay informed without opening the website. | High | Medium |
| 3 | Wishlist | Add a Save button to packages and destinations, with a My Saved Trips page. | People can return to trips they liked. | High | Small |
| 4 | Better package filters | Add budget and duration filters, plus sorting options, alongside the existing search, hotel, and transport filters. | Travellers find suitable trips faster. | High | Small |
| 5 | Group trip progress | Show whether a group trip is upcoming, ongoing, completed, or cancelled, separately from its approval decision. | Old groups no longer look like upcoming trips. | High | Medium |
| 6 | Ratings and reviews | Let travellers rate and describe a trip after completing a booking. | Future travellers can make a more informed choice. | High | Medium |
| 7 | Cancellation and refund requests | Let someone with a paid booking submit a request and track the decision and refund progress. | Replaces the current instruction to contact support with a trackable process. | High | Large |
| 8 | Admin activity history | Record which admin changed a booking, package, or group, and when. | The team can understand and investigate changes. | High | Medium |
| 9 | Photo galleries | Show several photos for each destination and package. | Travellers get a clearer idea of the experience. | Medium | Medium |
| 10 | Day-by-day itinerary | Expand the existing itinerary details into Day 1, Day 2, and so on, with activities and overnight stops. | Travellers know what to expect each day. | Medium | Medium |
| 11 | Booking receipt download | Let travellers download a receipt showing their booking, amount paid, and payment reference. | Gives travellers a convenient payment record. | Medium | Medium |
| 12 | Report export and manageable lists | Let admins download report data as CSV and browse long lists a page at a time. | Makes daily administration easier as records grow. | Medium | Medium |
| 13 | Departure dates and available seats | Let admins set package departure dates and seat limits, then show remaining availability. | Helps prevent accepting more bookings than a trip can hold. | Medium | Large |
| 14 | Support requests | Add a Help page where travellers can submit a question linked to their booking and follow replies. | Keeps support conversations in one place. | Medium | Medium |
| 15 | Package comparison | Let travellers compare two or three packages by price, duration, accommodation, transport, and included activities. | Makes choosing between similar trips easier. | Later | Medium |
| 16 | Staff permissions | Add a support role that can help travellers without being able to change every system setting. | Makes it easier to share admin work. | Later | Medium |
| 17 | English and Nepali language options | Let users switch the language of navigation, forms, and important messages. | Makes the system easier to use for different travellers. | Later | Large |
| 18 | Group chat improvements | Add pinned trip instructions, unread message counts, and a way to report inappropriate messages. | Keeps important information visible and gives users a way to raise concerns. | Later | Medium |

## 3. Recommended roadmap

The schedule below is a planning estimate for one developer familiar with this project, working regularly. Allow roughly 10-14 weeks for the core phases, with extra time for learning, service setup, or unexpected fixes. Treat the completion checks as the requirement for moving forward, even if a phase takes longer.

### Phase 1: Strengthen the existing system

**Suggested time: Week 1**

Before adding more booking features, address the issues already recorded in your audit.

- Read the session secret from environment configuration.
- Protect forms against unwanted submissions from another website.
- Limit repeated failed login attempts.
- Turn off debug mode by default outside local development.
- Add a dependency file so the project can be installed consistently.
- Rerun existing tests and investigate any failures; the older audit's results may have changed.

**Done when:** The app can be set up from documented instructions, the existing checks pass, and login and form protection have been verified.

### Phase 2: Help travellers return and stay informed

**Suggested time: Weeks 2-3**

- Build forgot password with an email link that expires and works only once.
- Set up booking confirmation and successful-payment emails using the same email service.
- Add the wishlist and My Saved Trips page.
- Add budget, duration, and sorting controls to the package list.

**Depends on:** Phase 1 and access to an email delivery service. Keep reminders for a later step if scheduled sending is not ready.

**Done when:** A traveller can reset a forgotten password, save and remove trips, filter packages, and receive an email after a confirmed booking or verified payment. Email failures must not undo a successful booking or payment.

### Phase 3: Make trip progress and feedback clear

**Suggested time: Weeks 4-5**

- Add a group trip end date or duration so the system knows when a trip ends.
- Separate approval status from trip progress; a rejected group should never automatically become a completed trip.
- Show upcoming and past groups consistently on traveller and admin pages.
- Record admin actions, including the previous value, new value, admin, and time.
- Add ratings and reviews for travellers whose bookings are marked completed.
- Allow admins to moderate abusive reviews, with the reason recorded.

**Depends on:** Clear trip dates and reliable booking completion records. Reviews should follow those rules.

**Done when:** Both roles see consistent trip progress, important admin changes have a history, and someone without a completed booking cannot submit a trip review. Prevent duplicate reviews for the same booking.

### Phase 4: Handle after-payment support

**Suggested time: Weeks 6-8**

- Define the cancellation deadline, approval rules, and possible refund amounts before building the screens.
- Add a paid-booking cancellation request form and admin decision page.
- Show separate states for request submitted, approved, refund pending, refunded, and rejected.
- Start with admin-handled refunds and record the payment provider reference. Add automated provider refunds only after checking the provider's supported process.
- Add downloadable receipts based on verified payments.
- Add support requests linked to bookings.

**Depends on:** Admin activity history and reliable payment records. Refunding money and changing a booking status must be treated as separate actions.

**Done when:** A traveller can track a request, an admin can record its resolution, and a booking is only shown as refunded after the refund is confirmed. Repeated actions must not issue a second refund.

### Phase 5: Improve trip information and administration

**Suggested time: Weeks 9-10**

- Add destination and package photo galleries.
- Expand package details into a day-by-day itinerary.
- Add CSV export to admin reports using the selected report filters.
- Add pagination to long admin lists while keeping their search and filters working.

**Depends on:** Agreement on the itinerary fields and the report columns to include.

**Done when:** Admins can manage multiple photos and itinerary days, travellers can view them comfortably on a phone, and exported totals match the filtered reports.

### Phase 6: Prepare a small release

**Suggested time: Weeks 11-12, with Weeks 13-14 as a buffer**

- Walk through registration, booking, payment, cancellation, group joining, chat, and reviews as both a traveller and an admin.
- Check mobile layouts, form labels, keyboard navigation, and useful error messages.
- Set up a production server, HTTPS, environment configuration, error alerts, and database backups.
- Test restoring a backup rather than only checking that a backup file exists.
- Document support contact details and booking, cancellation, and privacy information.
- Release to a small group of users, collect feedback, and fix the most common problems.

**Done when:** The main journeys work, payments have been checked in the provider's test environment, backups can be restored, and someone is responsible for responding to support requests and errors.

### Later releases: Add features based on actual demand

- **Departure dates and seats:** Prioritize this before launch if GoGather sells trips with limited seats. This would extend the schedule above. Reserve seats safely even when two people book at the same time, and release them on cancellation or expiry.
- **Scheduled reminders:** Add these once trip dates and email delivery are reliable; cancelled trips must not trigger reminders.
- **Package comparison:** Add when travellers regularly struggle to choose between packages.
- **Staff permissions:** Add when another person starts handling support or administration.
- **English and Nepali:** Translate the main booking journey first, then expand to the rest of the site.
- **Group chat improvements:** Add when active groups need better coordination and moderation.

## 4. Best scope if your time is limited

For a smaller project upgrade, complete Phase 1, then choose these five additions:

1. **Wishlist:** A simple feature that is easy to show in a demo.
2. **Better package filters:** Improves a journey your users already use.
3. **Forgot password:** Completes an important account feature.
4. **Group trip progress:** Makes existing group records easier to understand.
5. **Ratings and reviews:** Adds useful feedback once completed bookings are reliable.

Finish and verify each feature before starting the next. Keep payments, refunds, and seat availability as carefully planned workflows because they affect money and booking promises.

## 5. How to judge whether the additions help

- Can a traveller find a suitable package and save it without assistance?
- Can someone recover an account without asking an admin?
- Do travellers understand whether their booking is confirmed, paid, cancelled, or refunded?
- Can admins explain who changed a booking and why?
- Are completed trips clearly separated from upcoming trips?
- After the small release, are users completing bookings and asking fewer basic support questions?

Record feedback during the first release and use it to reorder later features. There is no need to build every idea before the system becomes useful.
