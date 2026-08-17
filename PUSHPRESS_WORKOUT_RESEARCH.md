"""
PushPress Workout API - Research Documentatie
==============================================

API Endpoints
-------------
Base:     https://api.pushpress.com/v2/graph/graphql
Login:    POST /v2/auth/login          {username, password}
Auth:     Bearer JWT (HS256, ~60 dagen)
Origin:   https://members.pushpress.com

Beschikbare GraphQL Queries (24 gevonden)
------------------------------------------
Query                                  Input
-------------------------------------  ------------------------------------------
getCalendarClassType(uuid)             {uuid}
getCalendarItems(...)                  {startDate, endDate, calendarSessionTypeId}
getClass(uuid)                         {uuid}
getClasses(...)                        {classDate, ...}
getClassTypes(date)                    {date}
getWorkoutOfDay(classTypeUid, date)    {classTypeUid, date}
getStaticWorkoutForDay(day, classTypeUid) {day, classTypeUid}
getWorkoutPart(workoutPartUid, scoreId) {workoutPartUid, scoreId}
workoutGetScores(workoutPartUid, workoutUid, classTypeId, date) {workoutPartUid, workoutUid, classTypeId, date}
getUpcomingReservations                {}
getProfile(clientUuid, userUuid)       {clientUuid, userUuid}
getLocations                           {}
getDocuments                           {}
getWorkingHours                        {}
getDiscounts                           {}
benchmarkWorkouts(input)               {input}
weightliftingWorkoutHistory            {}
getCalendarClassTypes                  {date}
getProfiles                            {clientUuid, userUuid}

Mutations: createReservation, cancelReservation

Workout Types (9 class types)
-----------------------------
ID      UID                                   Name
------  ------------------------------------  ---------------------
21528   4ebe07a3-b8f0-41ba-8e34-8d4cc2a09014  (unknown)
21527   8e5604a1-463b-4316-bce8-abdee466dabc  (unknown)
21533   a2002767-a66a-4894-a76e-4fe19bb33b20  (unknown)
21531   db209335-5cc6-4a9f-a611-64a10fe6b1b3  (unknown)
21534   54c6225b-bd35-432a-8bea-0299ff07870a  (unknown)
80454   a2641e45-ee92-467b-8e50-746cf4a57d5b  (unknown)
98562   84d97a3b-14b1-4efb-ae0c-6b7ba1b51438  (unknown)
134841  7800fabb-f1e4-48da-b6e9-d9de9a7f531c  (unknown)
113610  3c9fa768-dddd-4cbf-8044-6a94ccf6a092  (unknown)

Workout UIDs (gecorrect via getClassTypes + getCalendarClassTypes)
------------------------------------------------------------------
Class Type UID (gecorrect)                     Workout UID (gecorrect)
---------------------------------------------  ---------------------------------------------
4ebe07a3-b8f0-41ba-8e34-8d4cc2a09014           f75f0f4f-323b-4f97-869f-4627e121aed6  Classic CrossFit
8e5604a1-463b-4316-bce8-abdee466dabc           55f0fb1c-f512-4c1c-8222-7dcc52616c35  Functional CrossFit
a2002767-a66a-4894-a76e-4fe19bb33b20           ccd78983-ab3a-4904-b656-3d5ee94f3827  Olympic Weightlifting
db209335-5cc6-4a9f-a611-64a10fe6b1b3           222057ba-bddd-48a0-955e-401c2c805b84  Strength
54c6225b-bd35-432a-8bea-0299ff07870a           (geen workout)                        Advanced CrossFit
a2641e45-ee92-467b-8e50-746cf4a57d5b           (geen workout)                        Event
84d97a3b-14b1-4efb-ae0c-6b7ba1b51438           b63b3a59-43d1-43bc-8da6-cdf3d7cba3fc  Hyrox
7800fabb-f1e4-48da-b6e9-d9de9a7f531c           (geen workout)                        Boxes
3c9fa768-dddd-4cbf-8044-6a94ccf6a092           (geen workout)                        pre/post natal

Query Schema's
--------------

getWorkoutOfDay
Query:
  query GetWorkoutOfDay($classTypeUid: String!, $date: String!) {
    getWorkoutOfDay(getWorkoutOfDayInput: {classTypeUid: $classTypeUid, date: $date}) {
      uid
      workoutUid
      workoutState
      workoutProgramGroupId
      workoutProgramTemplateId
      imageUrl
      videoUrlId
      day
      __typename
    }
  }

Response type: WorkoutOfDay (array)
Fields:
  - uid: String
  - workoutUid: String
  - workoutState: String (PUBLISHED / UNPUBLISHED)
  - workoutProgramGroupId: String?
  - workoutProgramTemplateId: String?
  - imageUrl: String?
  - videoUrlId: String?
  - day: Int?

Resultaten (2026-08-13):
  - Classic CrossFit     -> f75f0f4f-323b-4f97-869f-4627e121aed6  ✅
  - Functional CrossFit  -> 55f0fb1c-f512-4c1c-8222-7dcc52616c35  ✅
  - Olympic Weightlifting-> ccd78983-ab3a-4904-b656-3d5ee94f3827  ✅
  - Strength             -> 222057ba-bddd-48a0-955e-401c2c805b84  ✅
  - Advanced CrossFit    -> geen workout                          ❌
  - Event                -> geen workout                          ❌
  - Hyrox                -> b63b3a59-43d1-43bc-8da6-cdf3d7cba3fc  ✅
  - Boxes                -> geen workout                          ❌
  - pre/post natal       -> geen workout                          ❌

getStaticWorkoutForDay
Query:
  query GetStaticWorkoutForDay($input: StaticWorkoutForDayInput!) {
    getStaticWorkoutForDay(input: {day: Int, classTypeUid: ID!}) {
      uid
      workoutUid
      workoutState
      workoutProgramGroupId
      workoutProgramTemplateId
      ...
    }
  }

Input: day (Int, 1-7 voor dag van de week?), classTypeUid (ID!)
Result: retourneert zelfde data als getWorkoutOfDay, maar met day parameter

getWorkoutPart
Query:
  query GetWorkoutPart($workoutPartUid: String!, $scoreId: Int) {
    getWorkoutPart(getWorkoutPartInput: {workoutPartUid: $workoutPartUid, scoreId: $scoreId}) {
      id
      workoutPartUid
      workoutUid
      sets
      tags
      scoreType
      athletesNotes
      coachesNotes
      scoreCount
      divisions
      defaultReps
      __typename
    }
  }

Input fields:
  - workoutPartUid (String!) - required
  - scoreId (Int) - optional

Response type: WorkoutOfDayParts
Fields:
  - id
  - workoutPartUid
  - workoutUid
  - sets (Float?)
  - tags: [String!]
  - scoreType
  - athletesNotes
  - coachesNotes
  - scoreCount
  - divisions
  - defaultReps

Resultaten: Alle geteste workoutPartUid waarden retourneren null fields:
  - 1, 2, 3, 4, 5
  - UUIDs van workouts
  - warmup, A, B, main

Probleem: De API accepteert de query maar retourneert geen data. Mogelijke oorzaken:
  1. Workout parts zijn niet gekoppeld aan deze workouts
  2. Admin heeft geen exercises toegevoegd
  3. Parts worden dynamisch gegenereerd (niet statisch)
  4. Alleen beschikbaar met een geldige scoreId

workoutGetScores
Query:
  query GetWorkoutScores($workoutPartUid: String!, $workoutUid: String, $classTypeId: Float, $date: String) {
    workoutGetScores(workoutGetScoresInput: {workoutPartUid, workoutUid, classTypeId, date}) {
      scores {
        id
        date
        division
        sets { weight, reps }
        mine
        athleteUid
        athleteComment
        workoutUid
        workoutPartUid
      }
      topScore { ... }
    }
  }

Response type: WorkoutPartScore
  - scores: [WorkoutLogScore!]
  - topScore: WorkoutLogScore
  - WorkoutLogScore heeft sets: [LogScoreSets!] met weight en reps

Resultaten: Alle queries retourneren lege scores arrays en topScore: null.
De gym heeft geen scores gelogd in PushPress.

Type Hierarchy (uit Flutter code gedecodeerd)
---------------------------------------------

WorkoutOfDay
├── uid: String
├── workoutUid: String
├── workoutState: String
├── workoutProgramGroupId: String?
├── workoutProgramTemplateId: String?
├── imageUrl: String?
├── videoUrlId: String?
├── day: Int?
│
├── WorkoutOfDayPart (via getWorkoutPart)
│   ├── id
│   ├── workoutPartUid
│   ├── workoutUid
│   ├── sets (Float?)
│   ├── tags: [String!]
│   ├── scoreType
│   ├── athletesNotes
│   ├── coachesNotes
│   ├── scoreCount
│   ├── divisions
│   └── defaultReps
│
├── WorkoutOfDayMedia
│   ├── mediaUrl
│   └── imageUrl
│
├── LogScoreSets
│   ├── weight
│   └── reps
│
├── WorkoutLogScore
│   ├── id
│   ├── date
│   ├── division
│   ├── classTypeId
│   ├── athleteDisplayName
│   ├── primaryScore
│   ├── secondaryScore
│   ├── athleteComment
│   ├── mine
│   ├── sets: [LogScoreSets]
│   ├── likes
│   └── comments
│
├── WorkoutComment
│   ├── id
│   ├── comment
│   ├── rawDate
│   ├── status
│   ├── media
│   ├── athlete
│   ├── likes
│   └── mine
│
├── WorkoutAthlete
│   ├── athleteUid
│   ├── firstName
│   ├── lastName
│   ├── id
│   └── profilePicture
│
├── WorkoutMedia
│   ├── mediaUrl
│   └── imageUrl
│
├── WorkoutSet
│   ├── weight
│   └── reps
│
└── WorkoutComment (re-used)
    ├── id
    ├── comment
    ├── rawDate
    ├── athlete
    ├── likes
    └── mine

Bekende Beperkingen
-------------------
1. Geen workout exercises in class schedule - getClass() retourneert geen workout data
2. Geen CalendarClassType workout fields - getCalendarClassType() heeft geen workoutUid/workoutParts velden
3. Geen getWorkout query - getWorkout(uuid) bestaat niet op de member API
4. Geen introspectie - Apollo Server introspection is disabled
5. BenchmarkWorkouts - timeout na 10s (mogelijk te veel data)
6. Geen admin API - geen admin endpoints gevonden in Flutter app
7. Workout parts zijn leeg - getWorkoutPart retourneert null voor alle geteste UIDs
8. Geen scores - workoutGetScores retourneert lege arrays

Implementatie Status
--------------------
- get_workout_of_day()  : IMPLEMENTEERD - retourneert workout metadata (uid, workoutUid, workoutState)
- get_workout_scores()  : IMPLEMENTEERD - retourneert scores arrays (momenteel leeg bij gym)

Voor echte workout exercises (warmup, A, B met sets/reps) is waarschijnlijk nodig:
  1. PushPress admin toegang, of
  2. Een ander systeem dat de gym gebruikt (bijv. Wodify, TrueCoach, etc.), of
  3. De workouts worden handmatig gedeeld via andere kanalen (WhatsApp, website, etc.)
"""
