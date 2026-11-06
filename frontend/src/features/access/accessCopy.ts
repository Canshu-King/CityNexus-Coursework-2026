/**
 * ORD-0436 -- the sentences the 成员与权限 screen says, in one file, both languages.
 *
 * Deliberately NOT in `i18n/LocaleContext.tsx`: that dictionary is the shared serialisation point
 * other lanes edit the same night -- the same reason `features/trash/journeyTrashCopy.ts` and
 * `features/studio/planVersionHistoryCopy.ts` give. Only `nav.access` went into the shared
 * dictionary, because the nav strip builds its labels as `t("nav." + key)` and has no fallback hook.
 *
 * Shape source: `D:/moodle/_apk_analysis_pi_travel/RELATION/relation_and_copy_findings.md` 1.4. What
 * is taken is the inventory -- which state gets a sentence, and which person that sentence names.
 * Every Chinese line here was rewritten for this product: it has trips, not 收藏夹.
 *
 * Three rules, all of them pinned elsewhere already:
 *  - a trip coming out of the bin is 找回. 恢复 and `recover*` are autonomous recovery here, and
 *    还原 is the plan-version screen's word (`planVersionHistoryCopy.ts`, restated by the 找回
 *    door in `backend/app/main.py`), so sharing does not take 还原 either: taking a person's access
 *    away is 撤销.
 *  - a sentence names whose situation changed. "对方将获得对应权限" and "你的权限也会随之变化"
 *    are two sentences because two people lose different things; which one applies is decided in
 *    `accessVocabulary.ts`, never by a string picked at the point of use.
 *  - nothing here offers what the server cannot do. There is no member table, no visibility column
 *    and no invitation door on this tree (ORD-0434 builds them), so the ladders say what each level
 *    will mean, `access.doorAbsent` says so on the same screen, and no ladder row is a control
 *    (ORD-0385: copy may not exceed the mechanism).
 */
export const accessCopy = {
  en: {
    "access.title": "Members and access",
    "access.sub":
      "Who can change this trip, what each visibility level means, and which of that this server can answer today.",
    "access.pickLabel": "Choose a trip",
    "access.pickNone": "Pick a trip to look at",
    /* A row with no title is named as that. The old control fell back to the trip's uuid, which
       is the one thing this list must never read out (ORD-0518: first and last row, by name). */
    "access.tripUntitled": "This trip came back with no name.",
    "access.pickLoading": "Reading your trip list…",
    "access.pickEmpty":
      "This server named no trip for you, so this screen has no subject yet. Nothing about your trips changed.",
    /* ORD-0477: the tier words.  `RECENT_UPDATE` / `DEPARTURE_DATE` / `TITLE` are the server's
       (`backend/app/schemas.py::JOURNEY_SORT_TIERS`) and never reach a traveller; these are the words
       the control renders, and the current tier is spelled out in its own sentence so the order a
       list is in is something the screen says rather than something the reader infers. */
    "access.sortHeading": "Order of this list",
    "access.sortCreation": "Creation order",
    "access.sortRecent": "Recently updated",
    "access.sortDeparture": "Departure date",
    "access.sortTitle": "Name",
    "access.sortCurrent": "This screen asked the server for: {tier}.",
    /* ORD-0518: two sentences, not one.  What this screen requested and what the door answered are
       different facts, and collapsing them is exactly how a screen ends up claiming a time order a
       server never gave.  `sortServerSilent` is the named absence; `sortMismatch` only renders when
       both halves are known and disagree. */
    "access.orderChecked": "Checked row by row: these rows are in this order ({reason}).",
    "access.orderBroken":
      "These rows are NOT in the order this screen asked for -- the order breaks at row {row}. The list below is what the server sent, not what the button says.",
    "access.orderUnverifiable":
      "This screen cannot check this order from the rows it was given ({reason}), so it does not claim the list is in it. The rows below are in the order they arrived.",
    /* Why the check compared what it compared, or why it compared nothing.  These are the whole
       sentence's one variable, and the reason lives here rather than in `journeyOrderVerdict.ts`: the
       module knows facts, this file knows the traveller's language.  The stored column names
       (`journeys.start_at`) and the code point behind a refused title go on the element as
       `data-compared` / `data-blocked-code-point` for the gate, not into a sentence. */
    "access.orderReasonStartAt": "what was compared is each trip's departure time",
    "access.orderReasonNoRows": "no trip came back at all",
    "access.orderReasonTitle": "what was compared is each trip's name",
    "access.orderReasonNoCreatedAt": "this door sends no created moment in a row",
    "access.orderReasonNoUpdatedAt": "this door sends no updated moment in a row",
    "access.orderReasonRowWithoutStart": "row {row} came back with no readable departure time",
    "access.orderReasonFoldDivergent":
      "a trip name here carries a character this screen cannot fold the way the server folded it",
    "access.noTierHeading": "Orders this screen cannot offer",
    "access.noTierArrival": "Arrival time",
    "access.noTierArrivalBecause":
      "The trip-list door has no arrival field. Arrival lives in the trip context's anchors, and no door on this server orders trips by it.",
    "access.noTierSaved": "Saved order",
    "access.noTierSavedBecause":
      "This server keeps no separate saved moment: a trip row carries when it was created and when it was updated, and both already have their own tier.",
    "access.sortTitleNote": "Names are ordered by characters, not by pinyin or by stroke count.",
    "access.selected": "Looking at: {title}",
    "access.ladderHeading": "Three roles, each with the one sentence that says what it can do",
    "access.roleOwner": "Owner",
    "access.roleAdmin": "Admin",
    "access.roleCollaborator": "Collaborator",
    "access.roleDetails": "Read the full permission rules",
    "access.roleLadderHeading": "What each role may do, read off this server's own ACL rows",
    "access.roleCapabilityCount": "This server grants this role {count} capabilities.",
    "access.capabilityTokens": "The capability codes, as the server names them",
    "access.scopeKeyUnread":
      "This server named a scope key this dictionary has no sentence for: {key}.",
    "access.scopeDoorSilent":
      "The role door answered nothing, so this screen lists no capabilities -- it does not guess them from the role name.",
    "access.roleChangeButton": "Change role",
    "access.selfExitButton": "Leave this trip",
    "access.roleChoicesHeading": "Move this seat to:",
    "access.roleChangePending":
      "This changes what that person may do on this trip. Your own access does not change.",
    "access.confirmSelfExit":
      "This takes you out of this trip. The row stays listed, so anybody reading the roster can see you left.",
    "access.adminCapOpen":
      "This server lets this trip hold {cap} admin seats, and {used} of them are taken right now.",
    "access.adminCapFull":
      "All {cap} admin seats this server allows are taken, so one further promotion is refused.",
    "access.adminCapUnanswered":
      "The seat door did not answer, so this screen says nothing about how many admins this trip may hold.",
    // ORD-0527: who this device is speaking for, said in the same breath as what it will not do.
    "access.identityLine":
      "This device is speaking for {name}, who is {role} on this trip.",
    "access.identityUnnamed":
      "The registry gave no name for the traveller this trip belongs to, so this line shows no id.",
    "access.identityUnknown":
      "This device does not know who you are: the trip this screen is on carries no traveller the server named.",
    "access.identityUnknownWay":
      "To go on, pick a trip above whose traveller this server did name -- this screen will not invent an identity to sign with.",
    "access.identityStale":
      "The trip reference this device stored matches nothing the list door answered, so nothing is loaded yet; pick a trip above.",
    "access.identityNoSeat":
      "This server has no membership row for you on this trip, so it lists no role-change or leaving control -- both doors would refuse.",
    "access.noAuthorityLine":
      "Your role does not hold member management on this server, so this screen lists no role-change control.",
    "access.memberScopeLine": "What this role can do: ",
    "access.roleDetailsBody":
      "A role is what this server lets that person do on this trip, not what a screen shows them. Removing the last owner is refused by the server rather than left for a traveller to discover.",
    "access.visibilityHeading": "Each visibility level, with its consequence said in the same breath",
    "access.visPrivate": "Only me",
    "access.visPrivateConsequence": "No one else can open it, not even holding the link.",
    "access.visLinkRead": "Anyone with the link",
    "access.visLinkReadConsequence": "Anyone holding the link can read it without being a member first.",
    "access.visInviteesOnly": "Invited people only",
    "access.visInviteesOnlyConsequence": "Only invitations still in date can open it; an expired one cannot.",
    "access.visPublic": "Public",
    "access.visPublicConsequence": "Visible to everyone on this network.",
    "access.confirmHeading": "What the screen will ask before it changes a permission",
    "access.whenOtherGainsOnly": "Someone else's role changes, yours stays",
    "access.whenActorLosesStanding": "You hand the trip to someone else",
    "access.whenRoleUnread": "This device could not read your own role",
    "access.whenMemberRemoved": "You take someone's access away",
    "access.confirmOtherGains": "That person will get the matching permission.",
    "access.confirmBothChange":
      "That person will get the matching permission, and your own permission changes with it.",
    "access.confirmUnanswered": "This screen will not answer that with a sentence it has to guess at.",
    "access.confirmRemoval": "That person will lose access to this trip.",
    "access.switchHeading": "Private to public, and public to private, each said from the other side",
    "access.switchPrivateToPublic":
      "After this the trip is visible to everyone -- people who could not see it before will be able to.",
    "access.switchPublicToPrivate":
      "After this, the people who followed it lose access -- they will not be able to open it any more.",
    "access.switchUnanswered": "This move has no gain-or-loss sentence on this tree, so none is shown.",
    "access.inviteHeading": "Invitations",
    "access.inviteExpires": "An invitation carries an expiry: {expires}. After that moment it does not open.",
    "access.inviteTtlMissing": "This server gave no expiry for an invitation, so none is claimed here.",
    "access.inviteQuotaNoCap":
      "This server gave a member count ({used}) and no limit, so nothing here says whether the trip is full.",
    "access.inviteQuotaMissing":
      "This server gave no member count or limit for this trip, so nothing is counted here.",
    "access.inviteLimitReached": "This trip has reached its member limit, so a new invitation cannot be sent.",
    "access.activityHeading": "What has happened on this trip",
    "access.activityLoading": "Reading what happened…",
    "access.activityEmpty": "This trip has no recorded action yet.",
    "access.action.journey.created": "created this trip",
    "access.action.journey.deleted": "put this trip in the recycle bin",
    "access.action.journey.undeleted": "brought this trip back from the recycle bin",
    "access.action.journey.memberAdded": "added {name} to this trip",
    "access.action.journey.memberRoleChanged": "changed {name}'s permission",
    "access.action.journey.memberRemoved": "took {name}'s access away",
    "access.memberUnnamed": "a member this server did not name",
    "access.action.journey.visibilityChanged": "changed who can see this trip",
    "access.action.journey.invitationSent": "sent an invitation to this trip",
    "access.action.journey.invitationRevoked": "cancelled an invitation to this trip",
    "access.actionUnknown": "This server reported an action this screen has no sentence for: {token}.",
    "access.doorLoading": "Asking this server who else can reach this trip…",
    "access.doorAbsent":
      "This server answered none of {paths}, so nothing on this screen can be changed today. The ladders say what each level will mean once those doors answer -- they are not a claim about this trip.",
    "access.doorPartial":
      "This server answered some but not all of the sharing doors; the ones still silent are {paths}. Read the ladders as what each level will mean, not as this trip's state.",
    "access.doorPresent":
      "This server answered all three sharing doors. The roster and the invitations below are its answers; the ladders and the confirmation lines are still this screen's own words.",
    "access.rosterHeading": "Members of this trip, as the member door answers",
    "access.rosterEmpty": "The member door answered with nobody in the list -- that is its answer, not a load failure.",
    "access.memberRow": "{role} · {name}",
    "access.memberNameUnread":
      "This server named nobody for that member id, so this row shows no name.",
    "access.memberStateActive": "still in force",
    "access.memberStateExpired": "past the expiry this server gave it",
    "access.memberStateRemoved": "withdrawn at {removed}",
    "access.memberStateUnknown": "This server gave a member state this screen has no sentence for: {state}.",
    "access.memberNoEnd": "carries no expiry, so it will not lapse on its own",
    "access.grantHeading": "Invitations issued for this trip",
    "access.grantEmpty": "The invitation door answered with none issued.",
    // ORD-0538: the way forward each empty cell offers.  Short on purpose -- these sit inside a
    // sentence at 390 wide, and a label long enough to push its own centre out of the hit area is the
    // exact defect the roadmap tells this batch to prove against.
    "access.emptyReloadActivity": "Re-read the actions",
    "access.emptyReloadRoles": "Re-read the role door",
    "access.emptyIssueGrant": "Issue a link-readable invitation",
    "access.emptyInviteSomeone": "Go to the invitation form",
    "access.roleUnread": "This server named a role this screen has no sentence for: {role}.",
    "access.grantTokenSummary": "The token this invitation carries",
    "access.grantActive": "Open until {expires}.",
    "access.grantNoExpiry": "This invitation carries no expiry, so it will not lapse on its own.",
    "access.grantRevoked": "Withdrawn at {revoked}. Whoever held this link can no longer open it.",
    "access.grantToken": "Invitation token, as the server gave it: {token}",
    "access.inviteFormHeading": "Issue an invitation with an expiry",
    "access.inviteFormPick": "Which visibility the invitation carries",
    "access.inviteFormExpiry": "Expiry moment (leaving it empty means the invitation never lapses)",
    "access.inviteFormSigner": "Signed with this trip's own traveler_id. This step is not authenticated: the server records what this device said, it does not check who typed it.",
    "access.inviteFormSubmit": "Issue the invitation",
    "access.inviteFormBusy": "Sending…",
    "access.confirmRevokeGrant": "After this, whoever holds the link can no longer open it. Your own access does not change.",
    "access.confirmRevokeMember": "After this, that person is no longer a member of this trip. Your own access does not change.",
    "access.revokeButton": "Withdraw",
    "access.revokeConfirmButton": "Confirm withdrawal",
    "access.writeRefusedUnknown": "The server refused this write. The code it answered with is: {code}.",
    "access.refuseExpiryInPast": "That expiry moment is already behind us, so the server did not issue the invitation.",
    "access.refuseExpiryNoOffset": "The expiry has to carry its offset from UTC; without one the server will not guess a deadline.",
    "access.refuseExpiryUnreadable": "The server could not read the expiry moment at all.",
    "access.refuseNotLinkReadable": "The server will not issue this level as a link: it says that level is not the link-readable kind.",
    "access.refuseJourneyGone": "This trip is gone from the server, so the invitation had nowhere to attach to.",
    "access.refuseGrantAlreadyRevoked": "This one was already withdrawn; the server did not touch it again.",
    "access.refuseGrantUnknown": "This trip has no such invitation on the server, so nothing was withdrawn.",
    "access.refuseMemberAlreadyWithdrawn": "That member already left; the server did not touch the row again.",
    "access.refuseMemberUnknown": "This trip has no such member on the server, so nothing was withdrawn.",
    "access.refuseRoleUnchanged": "That is already the role this member holds, so the server changed nothing.",
    "access.refuseRoleSelfChange": "The server will not let a person move their own seat; somebody else has to make that change.",
    "access.refuseOwnerRoleImmutable": "The owner seat does not move through this door in either direction, so a role change can never leave this trip unowned.",
    "access.refuseMemberManageNotPermitted": "The role you signed this request with does not hold member management on this trip, so the server refused to move that seat.",
    "access.refuseActorNotInJourney": "The person this request was signed as is not a member of this trip, so the server answered nothing.",
    "access.refuseActorLapsed": "That person's membership on this trip has run out, so it carries no authority to change a role.",
    "access.refuseAdminCapReached": "This trip already holds the number of admin seats the server allows, so the promotion was refused.",
    "access.refuseSelfExitNotAMember": "This server has no membership row for you on this trip, so there was nothing for you to leave.",
    "access.refuseOwnerCannotLeave": "You are this trip's owner, and the server will not let it be left unowned from this screen.",
    "access.activityNoSharingWriter":
      "Member and invitation actions are not written into this trip's activity stream by this server yet, so the list above holds only the trip's own actions.",
  },
  "zh-CN": {
    "access.title": "成员与权限",
    "access.sub": "谁能改这条行程、每个可见档位各意味着什么，以及这些里头哪些是这台服务器现在真能答的。",
    "access.pickLabel": "选一条行程",
    "access.pickNone": "先选一条要看的行程",
    "access.tripUntitled": "这条行程回来时没有名字。",
    "access.pickLoading": "正在读你的行程列表…",
    "access.pickEmpty": "这台服务器没有报出你的行程，这一屏还没有主语。你的行程没有因此变化。",
    /* ORD-0477: 档位的名字。旅客读的这些词与服务器的 CREATION_ORDER / RECENT_UPDATE /
       DEPARTURE_DATE / TITLE 一一对应（`backend/app/schemas.py::JOURNEY_SORT_TIERS`），机器词从不上屏。 */
    "access.sortHeading": "这一列行程怎么排",
    "access.sortCreation": "创建序",
    "access.sortRecent": "最近更新",
    "access.sortDeparture": "出发日期",
    "access.sortTitle": "名称",
    "access.sortCurrent": "这一屏向服务器要的是：{tier}。",
    /* ORD-0518: 两句而不是一句。这一屏要了什么、服务器答了什么，是两件事；把它们并成一句，
       就是屏上出现「服务器没给的顺序也被说成按时间排了」那条路。 */
    "access.orderChecked": "逐行核过：这些行确实是这个顺序（{reason}）。",
    "access.orderBroken": "这些行不是这一屏要的那个顺序——从第 {row} 行起对不上。下面列的是服务器发来的样子，不是按钮说的那个序。",
    "access.orderUnverifiable": "这一档这一屏核不了（{reason}），所以它不敢说这一列就是这个序；下面写的就是行收到的样子。",
    "access.orderReasonStartAt": "核的是每一程的出发时间",
    "access.orderReasonNoRows": "一行行程都没有回来",
    "access.orderReasonTitle": "核的是行程名",
    "access.orderReasonNoCreatedAt": "这一列里没有创建时间可比",
    "access.orderReasonNoUpdatedAt": "这一列里没有更新时间可比",
    "access.orderReasonRowWithoutStart": "第 {row} 行没有可读的出发时间",
    "access.orderReasonFoldDivergent": "有的行程名里带着这个屏折不出服务器那个序的字",
    "access.noTierHeading": "这一屏给不出的排法",
    "access.noTierArrival": "抵达时间序",
    "access.noTierArrivalBecause": "行程列表门没有抵达字段：抵达时刻在行程上下文的锚里，这台服务器没有任何门按它排行程。",
    "access.noTierSaved": "已存序",
    "access.noTierSavedBecause": "这台服务器没有另一个叫「已存」的时刻：行程行上只有创建与更新两个时刻，它们各自已经有档了。",
    "access.sortTitleNote": "名称档按字符先后排，不按拼音，也不按笔画。",
    "access.selected": "现在在看：{title}",
    "access.ladderHeading": "三档角色，每档一句它能做什么",
    "access.roleOwner": "所有者",
    "access.roleAdmin": "管理员",
    "access.roleCollaborator": "协作者",
    "access.roleDetails": "查看完整权限规则",
    "access.roleLadderHeading": "每一档能做什么，按这台服务器自己的 ACL 行读出来的",
    "access.roleCapabilityCount": "这台服务器给这一档 {count} 项能力。",
    "access.capabilityTokens": "能力编号（服务器原样叫法）",
    "access.scopeKeyUnread":
      "这台服务器报了一个本字典没有句子的作用域键：{key}。",
    "access.scopeDoorSilent":
      "角色那扇门没答，所以这一屏不列能力——不拿角色名去猜它能做什么。",
    "access.roleChangeButton": "改角色",
    "access.selfExitButton": "我自己退出",
    "access.roleChoicesHeading": "把这枚席位改成：",
    "access.roleChangePending": "这改的是那个人在这条行程里能做的事。你自己的权限不变。",
    "access.confirmSelfExit": "这把你从这条行程里移出去。这一行还留在名单上，别人读得到你走过。",
    "access.adminCapOpen": "这台服务器允许这条行程同时占 {cap} 枚管理员席位，现在占了 {used} 枚。",
    "access.adminCapFull": "这台服务器允许的 {cap} 枚管理员席位已经占满，再多给一枚会被拒。",
    "access.adminCapUnanswered": "席位那扇门没答，所以这一屏不说这条行程能放几枚管理员。",
    // ORD-0527：这台设备替谁说话，以及它不替谁去做的事，同一句里说完。
    "access.identityLine": "这台设备替「{name}」说话，他在这条行程上是{role}。",
    "access.identityUnnamed": "服务器没给这条行程所属的旅行者登记名字，所以这句不写编号。",
    "access.identityUnknown": "这台设备还不知道你是谁：这一屏所在的行程，服务器没有写出它属于哪位旅行者。",
    "access.identityUnknownWay": "要往下走，先在上方挑一条服务器写了旅行者的行程——这一屏不会自己编一个身份来签名。",
    "access.identityStale": "这台设备存的行程号在服务器的名单里没有对应，所以什么都没加载；请在上方重新挑一条行程。",
    "access.identityNoSeat": "服务器在这条行程上没有你的成员行，所以这里不列改角色与退出的按钮——那两扇门都会拒绝。",
    "access.noAuthorityLine": "你的档位在这台服务器上没有成员管理权，所以这里不列改角色的按钮。",
    "access.memberScopeLine": "这一档能做什么：",
    "access.roleDetailsBody":
      "角色说的是服务器允许这个人在这条行程上做什么，不是屏上显示成什么样。最后一个所有者不能被移除，这条由服务器拒绝，而不是让旅行者自己撞上才知道。",
    "access.visibilityHeading": "可见性每一档，后果就在同一句里",
    "access.visPrivate": "仅个人",
    "access.visPrivateConsequence": "别人打不开，拿到链接也打不开。",
    "access.visLinkRead": "获得链接者可见",
    "access.visLinkReadConsequence": "有链接的人不用先进成员名单就能读。",
    "access.visInviteesOnly": "受邀者可见",
    "access.visInviteesOnlyConsequence": "只有邀请还在有效期内的人打得开，过期就打不开了。",
    "access.visPublic": "公开",
    "access.visPublicConsequence": "全网公开可见。",
    "access.confirmHeading": "改权限之前，屏会先这样问",
    "access.whenOtherGainsOnly": "只动对方的档位，你的不变",
    "access.whenActorLosesStanding": "你把这条行程交到别人手上",
    "access.whenRoleUnread": "这台设备没读到你自己的档位",
    "access.whenMemberRemoved": "你撤销某个人的访问权限",
    "access.confirmOtherGains": "对方将获得对应权限。",
    "access.confirmBothChange": "对方将获得对应权限，你的权限也会随之变化。",
    "access.confirmUnanswered": "这一屏不肯用猜来的句子回答这一步。",
    "access.confirmRemoval": "对方将失去这条行程的权限。",
    "access.switchHeading": "私到公、公到私，各写一句对方会怎样",
    "access.switchPrivateToPublic": "转换后这条行程全网可见——原先看不见的人会开始能看见。",
    "access.switchPublicToPrivate": "转换后，已经在跟踪这条行程的人会失去访问权限。",
    "access.switchUnanswered": "这一步在这棵树上没有谁得失的句子，所以这里也不写一句。",
    "access.inviteHeading": "邀请",
    "access.inviteExpires": "邀请带着一个到期时刻：{expires}。过了那个时间就打不开了。",
    "access.inviteTtlMissing": "这台服务器没有给出邀请的有效期，所以这里不声称有一个。",
    "access.inviteQuotaNoCap":
      "这台服务器给了成员数（{used}），没给上限，所以这里不说这条行程满没满。",
    "access.inviteQuotaMissing": "这台服务器没有给出这条行程的成员数与上限，所以这里不计数。",
    "access.inviteLimitReached": "这条行程的成员已到上限，发不出新邀请。",
    "access.activityHeading": "这条行程上发生过什么",
    "access.activityLoading": "正在读发生过什么…",
    "access.activityEmpty": "这条行程还没有记下任何动作。",
    "access.action.journey.created": "新建了这条行程",
    "access.action.journey.deleted": "把这条行程丢进了回收站",
    "access.action.journey.undeleted": "把这条行程从回收站找回",
    "access.action.journey.memberAdded": "把{name}加进了这条行程",
    "access.action.journey.memberRoleChanged": "改了{name}的权限",
    "access.action.journey.memberRemoved": "撤销了{name}的访问权限",
    "access.memberUnnamed": "这台服务器没有点名的人",
    "access.action.journey.visibilityChanged": "改了这条行程能被谁看见",
    "access.action.journey.invitationSent": "向这条行程发出一个邀请",
    "access.action.journey.invitationRevoked": "撤销了这条行程的一个邀请",
    "access.actionUnknown": "这台服务器报了一个本屏说不出人话的动作：{token}。",
    "access.doorLoading": "正在问这台服务器还有谁能到达这条行程…",
    "access.doorAbsent":
      "这台服务器 {paths} 一扇都没答，所以这一屏今天没有任何一项能真改。下面写的是那几扇门答话之后每一档意味着什么，不是对这条行程下的结论。",
    "access.doorPartial":
      "这台服务器只答了共享那几扇门里的一部分，还没答的是 {paths}。下面写的是每档将要意味着什么，不是这条行程现在的状态。",
    "access.doorPresent":
      "这台服务器三扇共享门都答了。下面的名单与邀请是这几扇的答案，档位表和确认句仍是这一屏自己的话。",
    "access.rosterHeading": "这条行程的成员，按成员门答的",
    "access.rosterEmpty": "成员门答了，名单是空的——这是它的答案，不是没读到。",
    "access.memberRow": "{role} · {name}",
    "access.memberNameUnread":
      "这台服务器说没人为那个成员编号登记名字，所以这一行不显示名字。",
    "access.memberStateActive": "还在生效",
    "access.memberStateExpired": "已过它自己的到期时刻",
    "access.memberStateRemoved": "已在 {removed} 撤回",
    "access.memberStateUnknown": "这台服务器报了一个本屏说不出人话的成员状态：{state}。",
    "access.memberNoEnd": "没给到期时刻，也就是不会自己失效",
    "access.grantHeading": "这条行程发出去的邀请",
    "access.grantEmpty": "邀请门答了，一条也没发过。",
    // ORD-0538：每一处空态给一条走得动的路。字短是有意的——这些按钮坐在 390 宽的一句话里，
    // 标签长到把自己的中心推出命中区，正是这批要求被验的那一事。
    "access.emptyReloadActivity": "重读一次活动",
    "access.emptyReloadRoles": "重读角色门",
    "access.emptyIssueGrant": "发一条凭链接可读的邀请",
    "access.emptyInviteSomeone": "去邀请那一格",
    "access.roleUnread": "这台服务器报了一个本屏说不出人话的角色：{role}。",
    "access.grantTokenSummary": "这条邀请带的令牌",
    "access.grantActive": "到 {expires} 之前可开。",
    "access.grantNoExpiry": "这条邀请没有到期时刻，不会自己失效。",
    "access.grantRevoked": "已于 {revoked} 撤销。拿这条链接的人再也打不开了。",
    "access.grantToken": "邀请令牌（服务器原样给的）：{token}",
    "access.inviteFormHeading": "发一条带有效期的邀请",
    "access.inviteFormPick": "这条邀请带哪一档可见性",
    "access.inviteFormExpiry": "到期时刻（留空就是这条不会自己失效）",
    "access.inviteFormSigner": "签名用的是这条行程自己的 traveler_id。这一步不鉴权：服务器记下这台设备说了什么，不查是谁在敲。",
    "access.inviteFormSubmit": "发出邀请",
    "access.inviteFormBusy": "正在发…",
    "access.confirmRevokeGrant": "撤销后，拿这条链接的人就打不开了。你自己的权限不变。",
    "access.confirmRevokeMember": "撤回后，这个人不再是这条行程的成员。你自己的权限不变。",
    "access.revokeButton": "撤销",
    "access.revokeConfirmButton": "确认撤销",
    "access.writeRefusedUnknown": "这台服务器拒绝了这一笔，它给的编号是：{code}。",
    "access.refuseExpiryInPast": "那个到期时刻已经过去了，服务器没有发出这条邀请。",
    "access.refuseExpiryNoOffset": "到期时刻要带上与 UTC 的偏移，不带的话服务器不替你猜一个期限。",
    "access.refuseExpiryUnreadable": "服务器完全读不出这个到期时刻。",
    "access.refuseNotLinkReadable": "这一档不能当链接发出去：服务器说它不是可凭链接读的那一档。",
    "access.refuseJourneyGone": "这条行程已经不在服务器上了，邀请没地方挂。",
    "access.refuseGrantAlreadyRevoked": "这一条早就撤销了，服务器没有再动它。",
    "access.refuseGrantUnknown": "这条行程在服务器上没有这条邀请，所以什么都没撤销。",
    "access.refuseMemberAlreadyWithdrawn": "这一位早就退出了，服务器没有再动这一行。",
    "access.refuseMemberUnknown": "这条行程在服务器上没有这一位成员，所以什么都没撤回。",
    "access.refuseRoleUnchanged": "那本来就是这一位在服务器上的角色，所以服务器什么都没改。",
    "access.refuseRoleSelfChange": "服务器不许一个人自己挪自己的席位，这一改得由别人来按。",
    "access.refuseOwnerRoleImmutable": "主人席位不从这扇门两头挪，所以改角色不会让这条行程没有主人。",
    "access.refuseMemberManageNotPermitted": "你签名用的那个角色在这条行程上没有成员管理权，所以服务器没让你挪这一席。",
    "access.refuseActorNotInJourney": "这笔请求签名的人在服务器上不属这条行程的成员，所以服务器什么都没答。",
    "access.refuseActorLapsed": "那个人在这条行程上的成员期限已过，所以他没有改别人角色的权。",
    "access.refuseAdminCapReached": "这条行程的管理员席位已经占满服务器允许的数量，所以这一枚提升被拒。",
    "access.refuseSelfExitNotAMember": "服务器在这条行程上没有你的成员行，所以也没有可退的位置。",
    "access.refuseOwnerCannotLeave": "你是这条行程的主人，服务器不许从这一屏把它变成没有主人。",
    "access.activityNoSharingWriter":
      "成员与邀请这类动作，这台服务器还没往活动流里写，所以上面只有这条行程自己的动作。",
  },
} as const;

export type AccessCopyKey = keyof (typeof accessCopy)["zh-CN"];

/** A key missing in one language must never reach a traveller as a raw key (the rule every lane copy
 *  module in this tree follows). */
export function accessCopyFor(locale: string, key: AccessCopyKey): string {
  const table = accessCopy[locale.startsWith("zh") ? "zh-CN" : "en"];
  return table[key] ?? accessCopy.en[key] ?? key;
}

/** `{name}`, `{ttl}`, `{used}`, `{cap}`, `{token}` and `{path}` come from a read or from the door
 *  this screen asked -- never from a number this file knows. A placeholder with no value stays
 *  visible as the placeholder, so a field the server did not send reads as missing instead of
 *  arriving as a finished sentence. */
export function fillAccess(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? `{${name}}`));
}
